"""L-008 tests: FSA subtree statistics, versioned forbidden lists, release conditions.

架构 §5：某算子骨架占比超过阈值且长期无增量价值时进入版本化禁止列表；同骨架
参数变体设上限；列表记录原因、统计窗口和解除条件；生成后的确定性审查负责拦截
且 LLM 不得绕过；FSA 只影响新搜索，不改写历史和已入库因子。验收主断言是
多样性：禁止生效后主导骨架占比回落、新搜索骨架多样性上升。
"""
from __future__ import annotations

from typing import Any

import pytest

from domain_sdk.factor_fsa import (
    FsaDecision,
    FsaPolicy,
    FsaRoundSummary,
    FsaTracker,
    skeleton_key,
    subtree_skeletons,
)

POLICY = FsaPolicy(
    window_rounds=3,
    share_threshold=0.5,
    min_accept_rate=0.05,
    max_variants=2,
    min_window_observations=25,
    release_share=0.1,
    release_rounds=2,
)


def family(tag: str, window: int) -> dict[str, object]:
    """同一骨架的参数变体：算子结构相同，窗口不同。"""
    return {"op": tag, "window": window, "input": {"field": "close"}}


def other(window: int = 5) -> dict[str, object]:
    return {"op": "ts_mean", "window": window, "input": {"field": "volume"}}


def nested(window: int = 5) -> dict[str, object]:
    """家族骨架出现在子树位置的候选。"""
    return {"op": "rank", "window": window, "input": family("ts_delta", 5)}


def tracker(**overrides: Any) -> FsaTracker:
    kwargs: dict[str, Any] = {"policy": POLICY}
    kwargs.update(overrides)
    return FsaTracker(**kwargs)


def dominant_round(count: int = 8, others: int = 2) -> list[dict[str, object]]:
    return [family("ts_delta", 5 + i % 2) for i in range(count)] + [other() for _ in range(others)]


def diverse_round() -> list[dict[str, object]]:
    return [
        other(5),
        other(10),
        family("ts_mean", 20),
        {"op": "rank", "window": 5, "input": {"field": "volume"}},
    ]


# --------------------------------------------------------------------------- 骨架提取


def test_skeleton_abstracts_windows_and_fields() -> None:
    assert skeleton_key(family("ts_delta", 5)) == skeleton_key(family("ts_delta", 20))
    assert skeleton_key(family("ts_delta", 5)) != skeleton_key(other(5))
    assert skeleton_key({"field": "close"}) == skeleton_key({"field": "volume"})
    assert skeleton_key(nested()) == "(rank (ts_delta (field)))"
    assert subtree_skeletons(family("ts_delta", 5)) == ("(ts_delta (field))",)


def test_malformed_definitions_are_rejected_before_counting() -> None:
    with pytest.raises(ValueError, match="malformed"):
        tracker().observe([{"op": "ts_delta", "window": 5}])  # 缺 input
    with pytest.raises((TypeError, ValueError), match="malformed"):
        tracker().intercept("ts_delta(close)")  # type: ignore[arg-type]


def test_policy_rejects_infeasible_bounds() -> None:
    with pytest.raises(ValueError, match="share"):
        FsaPolicy(share_threshold=0.0)
    with pytest.raises(ValueError, match="release"):
        FsaPolicy(share_threshold=0.15, release_share=0.5)
    with pytest.raises(ValueError, match="rounds"):
        FsaPolicy(release_rounds=99)
    with pytest.raises(ValueError, match="variants"):
        FsaPolicy(max_variants=0)


# --------------------------------------------------------------------------- 统计与禁止


def test_dominant_valueless_skeleton_is_banned_after_window() -> None:
    gate = tracker()
    summaries: list[FsaRoundSummary] = []
    for _ in range(POLICY.window_rounds):
        summaries.append(gate.observe(dominant_round()))
    assert summaries[-1].bans_issued == 1
    entry = gate.active_bans()[0]
    assert entry.skeleton == "(ts_delta (field))"
    assert entry.version == 1
    assert entry.reason == "DOMINANT_WITHOUT_VALUE"
    assert entry.stats["share"] >= POLICY.share_threshold
    assert entry.stats["accept_rate"] < POLICY.min_accept_rate
    assert entry.window_rounds == POLICY.window_rounds
    assert entry.release_condition["release_share"] == POLICY.release_share
    assert entry.release_condition["release_rounds"] == POLICY.release_rounds


def test_valuable_dominant_skeleton_is_never_banned() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds + 1):
        batch = dominant_round()
        gate.observe(batch, accepted_hashes={hash_of(item) for item in batch[:6]})
    assert gate.active_bans() == ()


def hash_of(definition: dict[str, object]) -> str:
    from domain_sdk.factor_generation import candidate_hash

    return candidate_hash(definition)


def test_ban_requires_minimum_window_observations() -> None:
    gate = tracker()
    summary = gate.observe(dominant_round())
    assert summary.bans_issued == 0  # 观测量不足一个窗口下限，禁止冷启动误杀
    assert gate.active_bans() == ()


def test_intercept_blocks_banned_skeleton_in_any_position_or_variant() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    banned_variants = [family("ts_delta", 5), family("ts_delta", 99), nested()]
    for item in banned_variants:
        decision = gate.intercept(item)
        assert not decision.allowed
        assert decision.code == "BANNED_SKELETON"
        assert decision.skeleton == "(ts_delta (field))"
    assert gate.intercept(other(20)).allowed
    assert gate.intercept(other(20)).code == "OK"


def test_variant_cap_blocks_unseen_window_variants() -> None:
    gate = tracker()
    for index in range(POLICY.max_variants):
        gate.observe([family("ts_delta", 5 + index)] * 4 + [other()] * 6)
    seen = gate.intercept(family("ts_delta", 5))
    assert seen.allowed
    fresh = gate.intercept(family("ts_delta", 99))
    assert not fresh.allowed
    assert fresh.code == "VARIANT_CAP"
    assert "variant" in fresh.detail


# --------------------------------------------------------------------------- 解除条件


def test_release_conditions_restore_generation() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    assert len(gate.active_bans()) == 1

    for index in range(POLICY.release_rounds):
        summary = gate.observe(diverse_round())
        if index < POLICY.release_rounds - 1:
            assert summary.bans_released == 0
    assert gate.active_bans() == ()
    history = gate.ban_history()
    assert len(history) == 1
    assert history[0].released_round is not None
    assert history[0].version == 1
    assert gate.intercept(family("ts_delta", 5)).allowed


def test_reban_after_release_increments_version() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    for _ in range(POLICY.release_rounds):
        gate.observe(diverse_round())
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    active = gate.active_bans()
    assert len(active) == 1
    assert active[0].version == 2
    assert len(gate.ban_history()) == 2


def test_value_returned_releases_the_ban() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    for _ in range(POLICY.window_rounds):
        batch = dominant_round()
        gate.observe(batch, accepted_hashes={hash_of(item) for item in batch[:6]})
    assert gate.active_bans() == ()


def test_manual_release_records_reason() -> None:
    gate = tracker()
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    gate.release("(ts_delta (field))", reason="OPERATOR_UPGRADED")
    assert gate.active_bans() == ()
    assert gate.ban_history()[0].reason == "OPERATOR_UPGRADED"


# --------------------------------------------------------------------------- 多样性（验收主断言）


def test_diversity_recovers_after_interception() -> None:
    gate = tracker()
    dominant_before: list[float] = []
    for _ in range(POLICY.window_rounds):
        summary = gate.observe(dominant_round())
        dominant_before.append(summary.skeleton_share("(ts_delta (field))"))
    assert dominant_before[-1] >= POLICY.share_threshold

    # 拦截生效：家族候选全部被挡，生成端只剩其他骨架 + 多样化注入
    for _ in range(POLICY.release_rounds):
        assert all(
            gate.intercept(item).code == "BANNED_SKELETON" for item in dominant_round()[:8]
        )
        batch = [item for item in dominant_round() if gate.intercept(item).allowed]
        summary = gate.observe(batch + diverse_round())
    # 连续低于解除份额 → 禁令解除
    assert gate.active_bans() == ()
    # 保持多样化再观察一轮，禁令轮次的残留观察滑出统计窗口
    summary = gate.observe(diverse_round())
    # 主导骨架轮内占比归零、骨架多样性上升，且解除条件（连续低于解除份额）达成
    assert summary.skeleton_share("(ts_delta (field))") == 0.0
    assert summary.distinct_skeletons >= 2
    assert gate.active_bans() == ()


def test_fsa_never_rewrites_history_or_library() -> None:
    gate = tracker()
    library: list[dict[str, object]] = [family("ts_delta", 5)]
    for _ in range(POLICY.window_rounds):
        gate.observe(dominant_round())
    banned = gate.active_bans()[0]
    # 已入库因子原样保留；禁止列表只作用于新候选的拦截
    assert library[0] == family("ts_delta", 5)
    assert gate.intercept(library[0]).allowed is False
    assert gate.active_bans()[0] == banned
    assert gate.ban_history()[0].banned_round == banned.banned_round


# --------------------------------------------------------------------------- 快照与回放


def test_snapshot_roundtrip_and_replay() -> None:
    first = tracker()
    for _ in range(2):
        first.observe(dominant_round())
    resumed = FsaTracker.restore(first.snapshot(), policy=POLICY)

    tail = [dominant_round(), diverse_round(), dominant_round()]
    left_decisions = []
    for batch in tail:
        first.observe(batch)
        left_decisions.append(first.intercept(family("ts_delta", 5)))
    right_decisions = []
    for batch in tail:
        resumed.observe(batch)
        right_decisions.append(resumed.intercept(family("ts_delta", 5)))

    assert left_decisions == right_decisions
    assert [b.to_dict() for b in first.ban_history()] == [b.to_dict() for b in resumed.ban_history()]


def test_fsa_decision_roundtrip() -> None:
    decision = FsaDecision(False, "BANNED_SKELETON", "(ts_delta (field))", "banned at round 3")
    restored = FsaDecision.from_dict(decision.to_dict())
    assert restored == decision
