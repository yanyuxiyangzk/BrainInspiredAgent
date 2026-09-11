"""L-004 tests: momentum tracking, adaptive window step and explore/exploit rebalancing.

多轮回放是验收主断言：同一 seed 与同一反馈序列必须逐轮复现候选、配额与状态，
且从任意轮快照恢复后继续推进与全程推进完全一致。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from domain_sdk import factor_adaptation
from domain_sdk.factor_adaptation import (
    DEFAULT_MAX_RATIOS,
    DEFAULT_MIN_RATIOS,
    AdaptiveFactorSearch,
    ExplorationPolicy,
    FactorSearchGovernor,
    FactorSearchState,
    GeneratedCandidate,
    GeneratedRound,
    RoundFeedback,
)
from domain_sdk.factor_generation import (
    DEFAULT_RATIOS,
    FactorCandidateFactory,
    FactorVocabulary,
    GenerationQuota,
    GenerationStrategy,
    ParentPool,
    candidate_hash,
)

VOCABULARY = FactorVocabulary(
    fields=("close", "volume", "high"),
    operators=("rank", "ts_mean", "ts_delta"),
    windows=(5, 10, 20),
)


class StaticProposer:
    """Deterministic mechanism proposer standing in for the LLM sub-agent."""

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, count: int) -> list[dict[str, object]]:
        self.calls += count
        return [
            {"op": "ts_delta", "window": 20, "input": {"field": "close"}},
            {"op": "ts_mean", "window": 10, "input": {"field": "high"}},
        ][:count]


def feedback(
    tested: dict[GenerationStrategy, int] | None = None,
    accepted: dict[GenerationStrategy, int] | None = None,
    *,
    duplicates: int = 0,
    generated: int = 0,
) -> RoundFeedback:
    tested_map = tested if tested is not None else {GenerationStrategy.MUTATE: 10}
    accepted_map = accepted if accepted is not None else {GenerationStrategy.MUTATE: 6}
    return RoundFeedback(
        tested_by_strategy=tested_map,
        accepted_by_strategy=accepted_map,
        duplicates=duplicates,
        generated=generated if generated else sum(tested_map.values()),
    )


def collect_windows(tree: object) -> list[int]:
    windows: list[int] = []
    assert isinstance(tree, dict)
    if "window" in tree:
        windows.append(int(tree["window"]))  # type: ignore[arg-type]
    if "input" in tree:
        windows.extend(collect_windows(tree["input"]))
    return windows


def fake_backtest(
    candidates: Iterable[Any],
) -> tuple[set[str], set[str]]:
    """Accept only the (rank, window=10) family so search dynamics are deterministic."""
    tested: set[str] = set()
    accepted: set[str] = set()
    for item in candidates:
        digest = candidate_hash(item.definition)
        tested.add(digest)
        if item.definition.get("op") == "rank" and item.definition.get("window") == 10:
            accepted.add(digest)
    return tested, accepted


async def drive(
    search: AdaptiveFactorSearch,
    pool: ParentPool,
    rounds: int,
    count: int = 12,
) -> list[tuple[GeneratedRound, FactorSearchState]]:
    trace: list[tuple[GeneratedRound, FactorSearchState]] = []
    for _ in range(rounds):
        generated = await search.run_round(count, pool)
        tested, accepted = fake_backtest(generated.candidates)
        for item in generated.candidates:
            if candidate_hash(item.definition) in accepted:
                pool.add(item.definition)
        trace.append((generated, search.observe(RoundFeedback.from_round(generated, tested, accepted))))
    return trace


def round_signature(generated: GeneratedRound) -> tuple[Any, ...]:
    return (
        [item.definition for item in generated.candidates],
        dict(generated.quota),
        generated.window_step,
        generated.duplicates,
    )


# --------------------------------------------------------------------------- 契约与校验


def test_policy_defaults_match_architecture_quota() -> None:
    policy = ExplorationPolicy()
    assert policy.min_ratios == DEFAULT_MIN_RATIOS
    assert policy.max_ratios == DEFAULT_MAX_RATIOS
    for strategy, base in DEFAULT_RATIOS.items():
        assert policy.min_ratios[strategy] <= base <= policy.max_ratios[strategy]


def test_policy_rejects_infeasible_bounds() -> None:
    with pytest.raises(ValueError, match="min"):
        ExplorationPolicy(
            min_ratios={**DEFAULT_MIN_RATIOS, GenerationStrategy.MUTATE: 0.5},
            max_ratios={**DEFAULT_MAX_RATIOS, GenerationStrategy.MUTATE: 0.4},
        )
    with pytest.raises(ValueError, match="sum"):
        ExplorationPolicy(
            min_ratios={strategy: 0.3 for strategy in GenerationStrategy},
            max_ratios=DEFAULT_MAX_RATIOS,
        )
    with pytest.raises(ValueError, match="exploration"):
        ExplorationPolicy(min_exploration_share=0.9, max_exploration_share=0.95)
    with pytest.raises(ValueError, match="step"):
        ExplorationPolicy(min_window_step=3, max_window_step=1)
    with pytest.raises(ValueError, match="alpha"):
        ExplorationPolicy(momentum_alpha=0.0)
    with pytest.raises(ValueError, match="tilt"):
        ExplorationPolicy(tilt_sensitivity=1.5)


def test_feedback_rejects_accepted_without_tested() -> None:
    with pytest.raises(ValueError, match="tested"):
        feedback(
            tested={GenerationStrategy.MUTATE: 2},
            accepted={GenerationStrategy.MUTATE: 3},
        )
    with pytest.raises(ValueError, match="duplicates"):
        RoundFeedback(
            tested_by_strategy={GenerationStrategy.MUTATE: 1},
            accepted_by_strategy={GenerationStrategy.MUTATE: 1},
            duplicates=3,
            generated=2,
        )


def test_feedback_rate_properties() -> None:
    empty = RoundFeedback(
        tested_by_strategy={}, accepted_by_strategy={}, duplicates=0, generated=0
    )
    assert empty.acceptance_rate == 0.0
    assert empty.duplicate_rate == 0.0
    mixed = feedback(
        tested={GenerationStrategy.MUTATE: 4, GenerationStrategy.LLM_MECHANISM: 6},
        accepted={GenerationStrategy.MUTATE: 1, GenerationStrategy.LLM_MECHANISM: 2},
        duplicates=5,
        generated=20,
    )
    assert mixed.total_tested == 10
    assert mixed.total_accepted == 3
    assert mixed.acceptance_rate == pytest.approx(0.3)
    assert mixed.duplicate_rate == pytest.approx(0.25)


# --------------------------------------------------------------------------- 动量与份额


def test_initial_state_uses_base_quota_and_minimum_step() -> None:
    governor = FactorSearchGovernor()
    state = governor.state
    assert state.rounds == 0
    assert state.momentum is None
    assert state.exploration_share == pytest.approx(0.35)  # 基线探索组总额
    assert state.window_step == 1
    assert state.zero_accept_streak == 0
    quota = governor.current_quota()
    for strategy, ratio in quota.ratios.items():
        assert ratio == pytest.approx(DEFAULT_RATIOS[strategy])


def test_observe_tracks_acceptance_rate_momentum() -> None:
    governor = FactorSearchGovernor()
    first = governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 3}))
    assert first.momentum == pytest.approx(0.3)
    second = governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 5}))
    assert second.momentum == pytest.approx(0.4)  # 0.5*0.5 + 0.5*0.3
    third = governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 1}))
    assert third.momentum == pytest.approx(0.25)  # 0.5*0.1 + 0.5*0.4
    assert third.rounds == 3


def test_zero_accept_streak_pushes_exploration_share_and_step_up() -> None:
    governor = FactorSearchGovernor()
    starving = feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 0})
    first = governor.observe(starving)
    assert first.exploration_share == pytest.approx(0.35)  # 未到停滞窗口
    assert first.window_step == 1
    governor.observe(starving)
    third = governor.observe(starving)
    assert third.exploration_share == pytest.approx(0.35 + ExplorationPolicy().max_adjust_step)
    assert third.window_step == 2  # 停滞触发放大步长
    fourth = governor.observe(starving)
    assert fourth.exploration_share == pytest.approx(0.35 + 2 * ExplorationPolicy().max_adjust_step)
    assert fourth.window_step == ExplorationPolicy().max_window_step
    fifth = governor.observe(starving)
    assert fifth.exploration_share <= ExplorationPolicy().max_exploration_share
    assert fifth.window_step == ExplorationPolicy().max_window_step  # 步长封顶


def test_high_acceptance_pulls_budget_toward_exploitation() -> None:
    governor = FactorSearchGovernor()
    policy = ExplorationPolicy()
    rewarding = feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 6})
    first = governor.observe(rewarding)
    assert first.exploration_share == pytest.approx(0.35 - policy.max_adjust_step)
    assert first.window_step == policy.min_window_step  # 收敛期回到细粒度步长
    second = governor.observe(rewarding)
    assert second.exploration_share == pytest.approx(policy.min_exploration_share)
    third = governor.observe(rewarding)
    assert third.exploration_share == pytest.approx(policy.min_exploration_share)
    quota = governor.current_quota()
    exploit = sum(quota.ratios[s] for s in (GenerationStrategy.MUTATE, GenerationStrategy.CROSSOVER, GenerationStrategy.PARAMETER_PERTURB))
    assert exploit == pytest.approx(1 - policy.min_exploration_share)


def test_duplicate_signal_shifts_toward_exploration_without_touching_momentum() -> None:
    governor = FactorSearchGovernor()
    exhausted = RoundFeedback(
        tested_by_strategy={},
        accepted_by_strategy={},
        duplicates=8,
        generated=10,
    )
    state = governor.observe(exhausted)
    assert state.momentum is None  # 无回测事实，不动量
    assert state.zero_accept_streak == 0
    assert state.exploration_share == pytest.approx(0.35 + ExplorationPolicy().duplicate_shift)


def test_mixed_signals_are_capped_per_round() -> None:
    governor = FactorSearchGovernor()
    policy = ExplorationPolicy()
    for _ in range(policy.stagnation_rounds - 1):
        governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 0}))
    capped = governor.observe(
        RoundFeedback(
            tested_by_strategy={GenerationStrategy.MUTATE: 10},
            accepted_by_strategy={GenerationStrategy.MUTATE: 0},
            duplicates=10,
            generated=10,
        )
    )
    assert capped.exploration_share == pytest.approx(0.35 + policy.max_adjust_step)


# --------------------------------------------------------------------------- 配额有界分配


@pytest.mark.asyncio
async def test_quota_stays_within_policy_bounds_under_extreme_feedback() -> None:
    search = AdaptiveFactorSearch(VOCABULARY, seed=17)
    pool = ParentPool(capacity=16)
    policy = ExplorationPolicy()
    for index in range(24):
        generated = await search.run_round(12, pool)
        tested, accepted = fake_backtest(generated.candidates)
        if index % 2 == 0:  # 丰歉交替，把探索份额逼到两端边界
            accepted = tested
        for item in generated.candidates:
            if candidate_hash(item.definition) in accepted:
                pool.add(item.definition)
        state = search.observe(RoundFeedback.from_round(generated, tested, accepted))
        quota = search.current_quota()
        assert abs(sum(quota.ratios.values()) - 1.0) < 1e-9
        for strategy, ratio in quota.ratios.items():
            assert policy.min_ratios[strategy] - 1e-9 <= ratio <= policy.max_ratios[strategy] + 1e-9
        assert policy.min_exploration_share - 1e-9 <= state.exploration_share <= policy.max_exploration_share + 1e-9
        assert policy.min_window_step <= state.window_step <= policy.max_window_step


def test_within_group_tilt_boosts_better_performing_mechanism() -> None:
    governor = FactorSearchGovernor()
    governor.observe(
        feedback(
            tested={
                GenerationStrategy.RANDOM_EXPLORE: 2,
                GenerationStrategy.LLM_MECHANISM: 4,
            },
            accepted={
                GenerationStrategy.RANDOM_EXPLORE: 2,
                GenerationStrategy.LLM_MECHANISM: 0,
            },
        )
    )
    quota = governor.current_quota()
    assert quota.ratios[GenerationStrategy.RANDOM_EXPLORE] > quota.ratios[GenerationStrategy.LLM_MECHANISM]


def test_untested_strategies_keep_base_group_weight() -> None:
    governor = FactorSearchGovernor()
    governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 5}))
    quota = governor.current_quota()
    explore_total = sum(
        quota.ratios[s] for s in (GenerationStrategy.RANDOM_EXPLORE, GenerationStrategy.LLM_MECHANISM)
    )
    within_llm = quota.ratios[GenerationStrategy.LLM_MECHANISM] / explore_total
    assert within_llm == pytest.approx(DEFAULT_RATIOS[GenerationStrategy.LLM_MECHANISM] / 0.35)


def test_governor_rejects_base_ratio_outside_policy() -> None:
    base = {**DEFAULT_RATIOS, GenerationStrategy.MUTATE: 0.5}
    with pytest.raises(ValueError, match="base"):
        FactorSearchGovernor(base_ratios=base)


# --------------------------------------------------------------------------- 工厂扩展


def test_factory_rejects_non_positive_window_step() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=1)
    with pytest.raises(ValueError, match="step"):
        factory.set_window_step(0)


@pytest.mark.asyncio
async def test_factory_window_step_extends_perturb_reach() -> None:
    wide = FactorVocabulary(
        fields=("close", "volume"), operators=("rank",), windows=(5, 10, 20, 40, 80)
    )
    pool = ParentPool(capacity=4)
    pool.add({"op": "rank", "window": 10, "input": {"field": "close"}})
    step_two = FactorCandidateFactory(wide, seed=11)
    step_two.set_window_step(2)
    candidates = await step_two.generate_with_strategy(200, GenerationStrategy.PARAMETER_PERTURB, pool)
    windows: list[int] = []
    for item in candidates:
        item_windows = collect_windows(item.definition)
        assert len(item_windows) == 1
        windows.append(item_windows[0])
    assert set(windows) <= {5, 20, 40}  # 从窗口 10 出发步长 2 的可达集
    assert 40 in windows  # 200 次抽样必达 +2 位置
    step_one = FactorCandidateFactory(wide, seed=11)
    near = await step_one.generate_with_strategy(50, GenerationStrategy.PARAMETER_PERTURB, pool)
    near_windows = {collect_windows(item.definition)[0] for item in near}
    assert near_windows <= {5, 20}  # 步长 1 的可达集不越界


@pytest.mark.asyncio
async def test_factory_rebind_quota_changes_distribution_without_reset() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=21, mechanism_proposer=StaticProposer())
    pool = ParentPool(capacity=4)
    pool.add({"op": "rank", "window": 5, "input": {"field": "close"}})
    factory.rebind_quota(
        GenerationQuota(
            {
                GenerationStrategy.MUTATE: 0.0,
                GenerationStrategy.CROSSOVER: 0.5,
                GenerationStrategy.PARAMETER_PERTURB: 0.2,
                GenerationStrategy.RANDOM_EXPLORE: 0.1,
                GenerationStrategy.LLM_MECHANISM: 0.2,
            }
        )
    )
    candidates = await factory.generate(10, pool)
    counts: dict[GenerationStrategy, int] = {}
    for item in candidates:
        counts[item.strategy] = counts.get(item.strategy, 0) + 1
    assert counts.get(GenerationStrategy.MUTATE, 0) == 0
    assert sum(counts.values()) == 10


# --------------------------------------------------------------------------- 多轮回放（验收主断言）


@pytest.mark.asyncio
async def test_multi_round_replay_reproduces_every_round() -> None:
    first = AdaptiveFactorSearch(VOCABULARY, seed=2026, mechanism_proposer=StaticProposer())
    second = AdaptiveFactorSearch(VOCABULARY, seed=2026, mechanism_proposer=StaticProposer())
    trace_left = await drive(first, ParentPool(capacity=16), rounds=8)
    trace_right = await drive(second, ParentPool(capacity=16), rounds=8)
    assert len(trace_left) == 8
    for (round_left, state_left), (round_right, state_right) in zip(trace_left, trace_right):
        assert round_signature(round_left) == round_signature(round_right)
        assert state_left == state_right


@pytest.mark.asyncio
async def test_multi_round_replay_resumes_from_snapshot() -> None:
    full_search = AdaptiveFactorSearch(VOCABULARY, seed=2026, mechanism_proposer=StaticProposer())
    full_pool = ParentPool(capacity=16)
    full_trace = await drive(full_search, full_pool, rounds=8)

    partial_search = AdaptiveFactorSearch(VOCABULARY, seed=2026, mechanism_proposer=StaticProposer())
    partial_pool = ParentPool(capacity=16)
    partial_trace = await drive(partial_search, partial_pool, rounds=3)
    assert [round_signature(r) for r, _ in partial_trace] == [round_signature(r) for r, _ in full_trace[:3]]

    resumed = AdaptiveFactorSearch.restore(
        partial_search.snapshot(), VOCABULARY, mechanism_proposer=StaticProposer()
    )
    assert resumed.state == partial_search.state
    resumed_tail = await drive(resumed, partial_pool, rounds=5)
    assert [round_signature(r) for r, _ in resumed_tail] == [round_signature(r) for r, _ in full_trace[3:]]
    assert [s for _, s in resumed_tail] == [s for _, s in full_trace[3:]]


@pytest.mark.asyncio
async def test_generated_round_records_actual_quota_step_and_duplicates() -> None:
    search = AdaptiveFactorSearch(VOCABULARY, seed=8, mechanism_proposer=StaticProposer())
    pool = ParentPool(capacity=16)
    pool.add({"op": "ts_delta", "window": 20, "input": {"field": "close"}})
    pool.add({"op": "ts_mean", "window": 10, "input": {"field": "high"}})
    generated = await search.run_round(10, pool)
    assert sum(generated.quota.values()) == 10
    assert generated.quota == search.current_quota().allocate(10)
    assert generated.window_step == 1
    assert generated.duplicates >= 2  # llm 机制提案与父本池重复，计入反馈信号


@pytest.mark.asyncio
async def test_mechanism_shortfall_is_recorded_as_fallback_quota() -> None:
    class ShortProposer:
        async def propose(self, count: int) -> list[dict[str, object]]:
            return [{"op": "ts_mean", "window": 20, "input": {"field": "close"}}]

    search = AdaptiveFactorSearch(VOCABULARY, seed=3, mechanism_proposer=ShortProposer())
    generated = await search.run_round(10, ParentPool(capacity=4))
    assert generated.quota[GenerationStrategy.LLM_MECHANISM] < search.current_quota().allocate(10)[
        GenerationStrategy.LLM_MECHANISM
    ]
    assert generated.quota[GenerationStrategy.RANDOM_EXPLORE] > 0


@pytest.mark.asyncio
async def test_seen_hashes_count_as_duplicates_for_exhaustion_signal() -> None:
    search = AdaptiveFactorSearch(VOCABULARY, seed=8, mechanism_proposer=StaticProposer())
    pool = ParentPool(capacity=16)
    pool.add({"op": "ts_delta", "window": 20, "input": {"field": "close"}})
    pool.add({"op": "ts_mean", "window": 10, "input": {"field": "high"}})
    baseline = await search.run_round(10, pool)
    replay = AdaptiveFactorSearch(VOCABULARY, seed=8, mechanism_proposer=StaticProposer())
    again = await replay.run_round(10, pool)
    assert again.duplicates == baseline.duplicates  # 不传历史集合时行为不变

    prior_hashes = {candidate_hash(item.definition) for item in baseline.candidates[:3]}
    pool_hashes = {candidate_hash(parent) for parent in pool.parents()}
    expected_base = 0
    expected_extra = 0
    seen: set[str] = set()
    for item in again.candidates:
        digest = candidate_hash(item.definition)
        if digest in seen or digest in pool_hashes:
            expected_base += 1
        elif digest in prior_hashes:
            expected_extra += 1
        seen.add(digest)
    assert again.duplicates == expected_base
    marked = await replay.run_round(10, pool, seen_hashes=prior_hashes)
    assert marked.duplicates == expected_base + expected_extra
    assert expected_extra > 0  # 历史集合确实贡献了新的重复信号


# --------------------------------------------------------------------------- 状态序列化


def test_state_roundtrip_through_dict() -> None:
    governor = FactorSearchGovernor()
    governor.observe(feedback(tested={GenerationStrategy.MUTATE: 10}, accepted={GenerationStrategy.MUTATE: 1}))
    governor.observe(
        RoundFeedback(
            tested_by_strategy={GenerationStrategy.LLM_MECHANISM: 4},
            accepted_by_strategy={GenerationStrategy.LLM_MECHANISM: 0},
            duplicates=6,
            generated=12,
        )
    )
    restored = FactorSearchState.from_dict(governor.state.to_dict())
    assert restored == governor.state


def test_restore_rejects_unknown_or_out_of_bounds_payload() -> None:
    with pytest.raises(ValueError, match="format"):
        FactorSearchState.from_dict({"format": 99})
    state = FactorSearchState(
        rounds=2,
        momentum=0.2,
        exploration_share=0.4,
        window_step=2,
        zero_accept_streak=1,
    )
    payload = state.to_dict()
    with pytest.raises(ValueError, match="momentum"):
        FactorSearchState.from_dict({**payload, "momentum": 1.5})
    with pytest.raises(ValueError, match="weights"):
        FactorSearchState.from_dict({**payload, "group_weights": {"mutate": 1.0}})


def test_governor_restore_rejects_state_outside_policy() -> None:
    payload = FactorSearchState(
        rounds=1,
        momentum=0.1,
        exploration_share=0.9,  # 超出默认策略探索份额上限
        window_step=1,
        zero_accept_streak=0,
    ).to_dict()
    with pytest.raises(ValueError, match="exploration"):
        FactorSearchGovernor.restore(payload)


# --------------------------------------------------------------------------- 校验分支


def test_policy_rejects_partial_bounds_and_exploitation_windows() -> None:
    with pytest.raises(ValueError, match="five strategies"):
        ExplorationPolicy(min_ratios={GenerationStrategy.MUTATE: 0.1})
    with pytest.raises(ValueError, match="exploitation"):
        ExplorationPolicy(
            min_ratios={
                **DEFAULT_MIN_RATIOS,
                GenerationStrategy.MUTATE: 0.35,
                GenerationStrategy.CROSSOVER: 0.35,
            }
        )
    with pytest.raises(ValueError, match="exploitation"):
        ExplorationPolicy(
            max_ratios={
                **DEFAULT_MAX_RATIOS,
                GenerationStrategy.MUTATE: 0.20,
                GenerationStrategy.CROSSOVER: 0.20,
                GenerationStrategy.PARAMETER_PERTURB: 0.10,
            }
        )
    with pytest.raises(ValueError, match="adjust steps"):
        ExplorationPolicy(max_adjust_step=0.0)
    with pytest.raises(ValueError, match="exploitation threshold"):
        ExplorationPolicy(exploitation_threshold=1.5)
    with pytest.raises(ValueError, match="duplicate threshold"):
        ExplorationPolicy(duplicate_threshold=1.5)


def test_state_constructor_rejects_invalid_fields() -> None:
    base = FactorSearchState(
        rounds=0, momentum=None, exploration_share=0.35, window_step=1, zero_accept_streak=0
    )
    weights = base.group_weights
    with pytest.raises(ValueError, match="round counters"):
        FactorSearchState(
            rounds=-1, momentum=None, exploration_share=0.35, window_step=1,
            zero_accept_streak=0, group_weights=weights,
        )
    with pytest.raises(ValueError, match="momentum"):
        FactorSearchState(
            rounds=0, momentum=1.5, exploration_share=0.35, window_step=1,
            zero_accept_streak=0, group_weights=weights,
        )
    with pytest.raises(ValueError, match="exploration share"):
        FactorSearchState(
            rounds=0, momentum=None, exploration_share=1.5, window_step=1,
            zero_accept_streak=0, group_weights=weights,
        )
    with pytest.raises(ValueError, match="window step"):
        FactorSearchState(
            rounds=0, momentum=None, exploration_share=0.35, window_step=0,
            zero_accept_streak=0, group_weights=weights,
        )
    with pytest.raises(ValueError, match="five strategies"):
        FactorSearchState(
            rounds=0, momentum=None, exploration_share=0.35, window_step=1,
            zero_accept_streak=0, group_weights={GenerationStrategy.MUTATE: 1.0},
        )


def test_state_from_dict_rejects_corrupt_fields() -> None:
    state = FactorSearchState(
        rounds=2, momentum=0.2, exploration_share=0.4, window_step=2, zero_accept_streak=1
    )
    payload = state.to_dict()
    with pytest.raises(ValueError, match="rounds"):
        FactorSearchState.from_dict({**payload, "rounds": True})
    with pytest.raises(ValueError, match="zero_accept_streak"):
        FactorSearchState.from_dict({**payload, "zero_accept_streak": -1})
    with pytest.raises(ValueError, match="momentum"):
        FactorSearchState.from_dict({**payload, "momentum": "high"})
    with pytest.raises(ValueError, match="exploration share"):
        FactorSearchState.from_dict({**payload, "exploration_share": "half"})
    with pytest.raises(TypeError, match="group weights"):
        FactorSearchState.from_dict({**payload, "group_weights": "nope"})
    with pytest.raises(ValueError, match="positive"):
        collapsed = {strategy.value: 0.5 for strategy in GenerationStrategy}
        collapsed[GenerationStrategy.PARAMETER_PERTURB.value] = 0.0
        FactorSearchState.from_dict({**payload, "group_weights": collapsed})
    with pytest.raises(ValueError, match="sum"):
        unbalanced = {strategy.value: 0.2 for strategy in GenerationStrategy}
        unbalanced[GenerationStrategy.MUTATE.value] = 0.9
        FactorSearchState.from_dict({**payload, "group_weights": unbalanced})


def test_feedback_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        RoundFeedback(
            tested_by_strategy={GenerationStrategy.MUTATE: -1},
            accepted_by_strategy={},
            duplicates=0,
            generated=0,
        )
    with pytest.raises(ValueError, match="non-negative"):
        RoundFeedback(
            tested_by_strategy={},
            accepted_by_strategy={GenerationStrategy.MUTATE: -1},
            duplicates=0,
            generated=0,
        )


def test_generated_round_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError, match="window step"):
        GeneratedRound(candidates=(), quota={}, window_step=0, duplicates=0)
    with pytest.raises(ValueError, match="non-negative"):
        GeneratedRound(
            candidates=(), quota={GenerationStrategy.MUTATE: -1}, window_step=1, duplicates=0
        )
    with pytest.raises(ValueError, match="non-negative"):
        GeneratedRound(candidates=(), quota={}, window_step=1, duplicates=-1)


def test_from_round_maps_hashes_back_to_strategies() -> None:
    winner = {"op": "rank", "window": 10, "input": {"field": "close"}}
    loser = {"op": "ts_mean", "window": 5, "input": {"field": "volume"}}
    round_result = GeneratedRound(
        candidates=(
            GeneratedCandidate(GenerationStrategy.MUTATE, winner),
            GeneratedCandidate(GenerationStrategy.MUTATE, dict(winner)),
            GeneratedCandidate(GenerationStrategy.CROSSOVER, loser),
        ),
        quota={GenerationStrategy.MUTATE: 2, GenerationStrategy.CROSSOVER: 1},
        window_step=1,
        duplicates=1,
    )
    result = RoundFeedback.from_round(
        round_result, [candidate_hash(winner)], [candidate_hash(winner)]
    )
    assert result.tested_by_strategy == {GenerationStrategy.MUTATE: 2}
    assert result.accepted_by_strategy == {GenerationStrategy.MUTATE: 2}
    assert result.duplicates == 1
    assert result.generated == 3


def test_governor_restore_rejects_step_outside_policy() -> None:
    payload = FactorSearchState(
        rounds=1, momentum=0.1, exploration_share=0.35, window_step=9, zero_accept_streak=0
    ).to_dict()
    with pytest.raises(ValueError, match="window step"):
        FactorSearchGovernor.restore(payload)


def test_governor_snapshot_roundtrip_and_accessors() -> None:
    governor = FactorSearchGovernor()
    governor.observe(feedback())
    restored = FactorSearchGovernor.restore(governor.snapshot())
    assert restored.state == governor.state
    assert restored.policy.momentum_alpha == pytest.approx(governor.policy.momentum_alpha)


@pytest.mark.asyncio
async def test_adaptive_search_accessors_and_invalid_inputs() -> None:
    search = AdaptiveFactorSearch(VOCABULARY, seed=1)
    assert search.policy.max_window_step == 3
    assert search.current_quota().allocate(4)[GenerationStrategy.LLM_MECHANISM] == 1
    with pytest.raises(ValueError, match="count"):
        await search.run_round(0, ParentPool(capacity=2))
    with pytest.raises(TypeError, match="seed"):
        AdaptiveFactorSearch.restore({"format": 1}, VOCABULARY)


def test_restore_refuses_to_silently_drop_mechanism_proposer() -> None:
    search = AdaptiveFactorSearch(VOCABULARY, seed=5, mechanism_proposer=StaticProposer())
    payload = search.snapshot()
    assert payload["mechanism_proposer"] is True
    with pytest.raises(ValueError, match="proposer"):
        AdaptiveFactorSearch.restore(payload, VOCABULARY, mechanism_proposer=None)
    plain = AdaptiveFactorSearch(VOCABULARY, seed=5)
    assert plain.snapshot()["mechanism_proposer"] is False
    AdaptiveFactorSearch.restore(plain.snapshot(), VOCABULARY)  # 无提案器快照可无提案器恢复


def test_bounded_proportional_raises_when_bounds_cannot_absorb_total() -> None:
    weights = {GenerationStrategy.MUTATE: 0.5, GenerationStrategy.CROSSOVER: 0.5}
    bounds = {strategy: (0.1, 0.3) for strategy in weights}
    with pytest.raises(ValueError, match="converge"):
        factor_adaptation._bounded_proportional(1.0, weights, bounds)


def test_tilt_weights_guard_keeps_group_when_adjustment_collapses() -> None:
    base = FactorSearchState(
        rounds=0, momentum=None, exploration_share=0.35, window_step=1, zero_accept_streak=0
    )
    weights = dict(base.group_weights)
    weights[GenerationStrategy.MUTATE] = 10.0
    feedback_signal = RoundFeedback(
        tested_by_strategy={GenerationStrategy.MUTATE: 1, GenerationStrategy.CROSSOVER: 1},
        accepted_by_strategy={GenerationStrategy.CROSSOVER: 1},
        duplicates=0,
        generated=2,
    )
    tilted = factor_adaptation._tilt_group_weights(weights, feedback_signal, sensitivity=3.0)
    assert tilted[GenerationStrategy.MUTATE] == weights[GenerationStrategy.MUTATE]
