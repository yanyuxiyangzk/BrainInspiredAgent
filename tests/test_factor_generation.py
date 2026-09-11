"""L-003 tests: five generation strategies, quotas, parent pool and seeded reproducibility."""
from __future__ import annotations

from typing import Any

import pytest

from domain_sdk.factor_generation import (
    DEFAULT_RATIOS,
    FactorCandidateFactory,
    FactorVocabulary,
    GenerationQuota,
    GenerationStrategy,
    ParentPool,
    candidate_hash,
    tree_leaves,
)

VOCABULARY = FactorVocabulary(
    fields=("close", "volume", "high"),
    operators=("rank", "ts_mean", "ts_delta"),
    windows=(5, 10, 20),
)


def parent() -> dict[str, object]:
    return {"op": "ts_mean", "window": 5, "input": {"field": "close"}}


def second_parent() -> dict[str, object]:
    return {"op": "rank", "window": 10, "input": {"field": "volume"}}


def seeded_pool() -> ParentPool:
    pool = ParentPool(capacity=8)
    pool.add(parent())
    pool.add(second_parent())
    return pool


def strategy_counts(candidates: list[Any]) -> dict[GenerationStrategy, int]:
    counts: dict[GenerationStrategy, int] = {}
    for item in candidates:
        counts[item.strategy] = counts.get(item.strategy, 0) + 1
    return counts


def assert_is_valid_tree(tree: object) -> None:
    assert isinstance(tree, dict)
    if "field" in tree:
        assert tree["field"] in VOCABULARY.fields
        return
    assert tree["op"] in VOCABULARY.operators
    assert tree["window"] in VOCABULARY.windows
    assert_is_valid_tree(tree["input"])


def collect_windows(tree: object) -> list[int]:
    windows: list[int] = []
    assert isinstance(tree, dict)
    if "window" in tree:
        windows.append(int(tree["window"]))  # type: ignore[arg-type]
    if "input" in tree:
        windows.extend(collect_windows(tree["input"]))
    return windows


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


class BrokenProposer:
    async def propose(self, count: int) -> list[dict[str, object]]:
        raise RuntimeError("llm unavailable")


def test_generation_quota_defaults_match_architecture() -> None:
    assert DEFAULT_RATIOS[GenerationStrategy.MUTATE] == 0.25
    assert DEFAULT_RATIOS[GenerationStrategy.CROSSOVER] == 0.25
    assert DEFAULT_RATIOS[GenerationStrategy.PARAMETER_PERTURB] == 0.15
    assert DEFAULT_RATIOS[GenerationStrategy.RANDOM_EXPLORE] == 0.15
    assert DEFAULT_RATIOS[GenerationStrategy.LLM_MECHANISM] == 0.20


def test_generation_quota_rejects_invalid_ratios() -> None:
    with pytest.raises(ValueError, match="all five"):
        GenerationQuota({GenerationStrategy.MUTATE: 1.0})
    with pytest.raises(ValueError, match="sum"):
        GenerationQuota({strategy: 0.5 for strategy in GenerationStrategy})
    with pytest.raises(ValueError, match="must stay within"):
        GenerationQuota({**DEFAULT_RATIOS, GenerationStrategy.MUTATE: 1.25})


def test_quota_allocation_is_deterministic_largest_remainder() -> None:
    quota = GenerationQuota(dict(DEFAULT_RATIOS))
    allocation = quota.allocate(10)
    assert allocation == {
        GenerationStrategy.MUTATE: 3,
        GenerationStrategy.CROSSOVER: 3,
        GenerationStrategy.PARAMETER_PERTURB: 1,
        GenerationStrategy.RANDOM_EXPLORE: 1,
        GenerationStrategy.LLM_MECHANISM: 2,
    }
    assert sum(allocation.values()) == 10
    assert quota.allocate(10) == allocation
    assert quota.allocate(0) == {strategy: 0 for strategy in GenerationStrategy}


def test_parent_pool_is_bounded_and_deduplicates() -> None:
    pool = ParentPool(capacity=2)
    assert pool.add(parent()) is True
    assert pool.add(parent()) is False  # 内容去重
    other = second_parent()
    assert pool.add(other) is True
    assert pool.add({"op": "rank", "input": {"field": "high"}}) is True  # 淘汰最旧
    assert len(pool) == 2
    assert parent() not in pool.parents()  # FIFO：最旧的父本被淘汰
    pool.extend([parent(), parent()])
    assert len(pool) == 2


def test_tree_leaves_returns_all_leaf_nodes() -> None:
    tree = {
        "op": "rank",
        "input": {"op": "ts_delta", "window": 10, "input": {"field": "close"}},
        "window": 20,
    }
    leaves = tree_leaves(tree)
    assert leaves == [{"field": "close"}]


def test_candidate_hash_is_stable() -> None:
    assert candidate_hash(parent()) == candidate_hash(
        {"input": {"field": "close"}, "op": "ts_mean", "window": 5}
    )
    assert candidate_hash(parent()) != candidate_hash({"op": "rank", "input": {"field": "close"}})


@pytest.mark.asyncio
async def test_default_quota_distribution_and_structured_output() -> None:
    proposer = StaticProposer()
    factory = FactorCandidateFactory(VOCABULARY, seed=42, mechanism_proposer=proposer)
    candidates = await factory.generate(10, seeded_pool())

    assert strategy_counts(candidates) == {
        GenerationStrategy.MUTATE: 3,
        GenerationStrategy.CROSSOVER: 3,
        GenerationStrategy.PARAMETER_PERTURB: 1,
        GenerationStrategy.RANDOM_EXPLORE: 1,
        GenerationStrategy.LLM_MECHANISM: 2,
    }
    assert proposer.calls == 2  # llm 配额逐个征询
    for item in candidates:
        assert_is_valid_tree(item.definition)


@pytest.mark.asyncio
async def test_cold_start_falls_back_to_random_and_mechanism() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=7, mechanism_proposer=StaticProposer())
    candidates = await factory.generate(10, ParentPool(capacity=4))
    counts = strategy_counts(candidates)
    assert counts.get(GenerationStrategy.MUTATE, 0) == 0
    assert counts.get(GenerationStrategy.CROSSOVER, 0) == 0
    assert counts.get(GenerationStrategy.PARAMETER_PERTURB, 0) == 0
    assert counts.get(GenerationStrategy.RANDOM_EXPLORE, 0) == 8  # 依赖父本的三类配额全部回退
    assert counts.get(GenerationStrategy.LLM_MECHANISM, 0) == 2
    for item in candidates:
        assert_is_valid_tree(item.definition)


@pytest.mark.asyncio
async def test_same_seed_reproduces_identical_candidates() -> None:
    first = FactorCandidateFactory(VOCABULARY, seed=99, mechanism_proposer=StaticProposer())
    second = FactorCandidateFactory(VOCABULARY, seed=99, mechanism_proposer=StaticProposer())
    left = await first.generate(12, seeded_pool())
    right = await second.generate(12, seeded_pool())
    assert [item.definition for item in left] == [item.definition for item in right]


@pytest.mark.asyncio
async def test_different_seed_diverges() -> None:
    first = FactorCandidateFactory(VOCABULARY, seed=1, mechanism_proposer=StaticProposer())
    second = FactorCandidateFactory(VOCABULARY, seed=2, mechanism_proposer=StaticProposer())
    left = await first.generate(12, seeded_pool())
    right = await second.generate(12, seeded_pool())
    assert [item.definition for item in left] != [item.definition for item in right]


@pytest.mark.asyncio
async def test_mutate_replaces_exactly_one_leaf() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=5)
    pool = ParentPool(capacity=4)
    pool.add(parent())
    candidates = await factory.generate_with_strategy(4, GenerationStrategy.MUTATE, pool)
    base = parent()
    for item in candidates:
        assert item.strategy is GenerationStrategy.MUTATE
        assert item.definition != base  # 确有变化
        assert item.definition["op"] == base["op"]  # 骨架保留，仅叶被替换
        assert_is_valid_tree(item.definition)


@pytest.mark.asyncio
async def test_perturb_shifts_only_windows_within_vocabulary() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=11)
    pool = ParentPool(capacity=4)
    pool.add(parent())
    candidates = await factory.generate_with_strategy(
        4, GenerationStrategy.PARAMETER_PERTURB, pool
    )
    for item in candidates:
        assert item.strategy is GenerationStrategy.PARAMETER_PERTURB
        assert item.definition != parent()  # 窗口必有位移
        windows = collect_windows(item.definition)
        assert windows and all(window in VOCABULARY.windows for window in windows)


@pytest.mark.asyncio
async def test_crossover_combines_two_parents() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=13)
    candidates = await factory.generate_with_strategy(4, GenerationStrategy.CROSSOVER, seeded_pool())
    for item in candidates:
        assert item.strategy is GenerationStrategy.CROSSOVER
        assert_is_valid_tree(item.definition)


@pytest.mark.asyncio
async def test_mechanism_proposer_failure_falls_back_to_random() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=3, mechanism_proposer=BrokenProposer())
    candidates = await factory.generate(4, ParentPool(capacity=2))
    counts = strategy_counts(candidates)
    assert counts[GenerationStrategy.RANDOM_EXPLORE] == 4
    assert counts.get(GenerationStrategy.LLM_MECHANISM, 0) == 0


@pytest.mark.asyncio
async def test_strategy_can_be_explicitly_disabled_by_quota() -> None:
    ratios: dict[GenerationStrategy, float] = {
        GenerationStrategy.MUTATE: 0.0,
        GenerationStrategy.CROSSOVER: 0.30,
        GenerationStrategy.PARAMETER_PERTURB: 0.20,
        GenerationStrategy.RANDOM_EXPLORE: 0.20,
        GenerationStrategy.LLM_MECHANISM: 0.30,
    }
    factory = FactorCandidateFactory(
        VOCABULARY, seed=8, quota=GenerationQuota(ratios), mechanism_proposer=StaticProposer()
    )
    candidates = await factory.generate(10, seeded_pool())
    counts = strategy_counts(candidates)
    assert counts.get(GenerationStrategy.MUTATE, 0) == 0
    assert sum(counts.values()) == 10


@pytest.mark.asyncio
async def test_factory_rejects_zero_count_and_vocabulary_errors() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=1)
    with pytest.raises(ValueError, match="count"):
        await factory.generate(0, seeded_pool())
    with pytest.raises(ValueError, match="fields"):
        FactorVocabulary(fields=(), operators=("rank",), windows=(5,))
    with pytest.raises(ValueError, match="fields"):
        FactorVocabulary(fields=("close",), operators=("rank",), windows=(5,))
    with pytest.raises(ValueError, match="windows"):
        FactorVocabulary(fields=("close", "volume"), operators=("rank",), windows=())
