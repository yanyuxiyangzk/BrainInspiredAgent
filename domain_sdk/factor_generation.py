"""Seeded, quota-driven candidate generation for the factor discovery loop (L-003).

The factory is a pure, deterministic library: it never backtests and never
writes the factor library. It draws parents from an injected bounded pool and
returns structured candidates labelled with the strategy that produced them.
``llm_mechanism`` candidates come from an injected proposer (the generation
sub-agent boundary); proposer failures fall back to random exploration so a
degraded model can never stall the loop.
"""
from __future__ import annotations

import copy
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from domain_sdk.factor_loop import FactorDiscoveryLoop


class GenerationStrategy(StrEnum):
    MUTATE = "mutate"
    CROSSOVER = "crossover"
    PARAMETER_PERTURB = "parameter_perturb"
    RANDOM_EXPLORE = "random_explore"
    LLM_MECHANISM = "llm_mechanism"


DEFAULT_RATIOS: Mapping[GenerationStrategy, float] = {
    GenerationStrategy.MUTATE: 0.25,
    GenerationStrategy.CROSSOVER: 0.25,
    GenerationStrategy.PARAMETER_PERTURB: 0.15,
    GenerationStrategy.RANDOM_EXPLORE: 0.15,
    GenerationStrategy.LLM_MECHANISM: 0.20,
}

_TREE_DEPTH_LIMIT = 3


@dataclass(frozen=True, slots=True)
class FactorVocabulary:
    fields: tuple[str, ...]
    operators: tuple[str, ...]
    windows: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(set(self.fields)) != len(self.fields) or len(self.fields) < 2:
            raise ValueError("fields must contain at least 2 unique entries")
        if not self.operators or len(set(self.operators)) != len(self.operators):
            raise ValueError("operators must contain at least 1 unique entry")
        if len(self.windows) < 2 or len(set(self.windows)) != len(self.windows):
            raise ValueError("windows must contain at least 2 unique entries")


@dataclass(frozen=True, slots=True)
class GenerationQuota:
    ratios: Mapping[GenerationStrategy, float]

    def __post_init__(self) -> None:
        if set(self.ratios) != set(GenerationStrategy):
            raise ValueError("all five strategy ratios are required")
        if any(ratio < 0 or ratio > 1 for ratio in self.ratios.values()):
            raise ValueError("ratios must stay within [0, 1]")
        if abs(sum(self.ratios.values()) - 1.0) > 1e-9:
            raise ValueError("ratios must sum to 1")

    def allocate(self, total: int) -> dict[GenerationStrategy, int]:
        """Deterministic largest-remainder allocation over declaration order."""
        if total < 0:
            raise ValueError("total must be non-negative")
        base = {strategy: int(total * self.ratios[strategy]) for strategy in GenerationStrategy}
        remaining = total - sum(base.values())
        order = list(GenerationStrategy)
        fractions = sorted(
            order,
            key=lambda strategy: (
                -(total * self.ratios[strategy] - base[strategy]),
                order.index(strategy),
            ),
        )
        for strategy in fractions[:remaining]:
            base[strategy] += 1
        return base


@dataclass(frozen=True, slots=True)
class GeneratedCandidate:
    strategy: GenerationStrategy
    definition: Mapping[str, object]


class ParentPool:
    """Bounded FIFO pool; content-addressed dedup keeps the search diversified."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("parent pool capacity must be positive")
        self._capacity = capacity
        self._entries: list[Mapping[str, object]] = []
        self._seen: set[str] = set()

    def add(self, candidate: Mapping[str, object]) -> bool:
        digest = FactorDiscoveryLoop.candidate_hash(candidate)
        if digest in self._seen:
            return False
        self._entries.append(candidate)
        self._seen.add(digest)
        while len(self._entries) > self._capacity:
            evicted = self._entries.pop(0)
            self._seen.discard(FactorDiscoveryLoop.candidate_hash(evicted))
        return True

    def extend(self, candidates: Sequence[Mapping[str, object]]) -> None:
        for candidate in candidates:
            self.add(candidate)

    def parents(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class MechanismProposer(Protocol):
    async def propose(self, count: int) -> Sequence[Mapping[str, object]]: ...


def candidate_hash(candidate: Mapping[str, object], *, algorithm_version: str = "1") -> str:
    return FactorDiscoveryLoop.candidate_hash(candidate, algorithm_version=algorithm_version)


def tree_leaves(tree: Mapping[str, object]) -> list[dict[str, object]]:
    """All leaf nodes (``{"field": ...}``) of an expression tree, in DFS order."""
    if "field" in tree:
        return [dict(tree)]
    leaves: list[dict[str, object]] = []
    child: object = tree.get("input")
    if isinstance(child, Mapping):
        leaves.extend(tree_leaves(child))
    return leaves


def _is_leaf(node: Mapping[str, object]) -> bool:
    return "field" in node


def _operator_nodes(tree: Mapping[str, object]) -> list[dict[str, object]]:
    """Live references to every operator node (including the root) along the chain."""
    if _is_leaf(tree):
        return []
    nodes: list[dict[str, object]] = [cast(dict[str, object], tree)]
    child: object = tree.get("input")
    if isinstance(child, Mapping):
        nodes.extend(_operator_nodes(child))
    return nodes


def _subtree_count(node: Mapping[str, object]) -> int:
    if _is_leaf(node) or "input" not in node:
        return 1
    child = node["input"]
    assert isinstance(child, Mapping)
    return 1 + _subtree_count(child)


def _node_at(node: Mapping[str, object], index: int) -> Mapping[str, object]:
    """The index-th subtree of ``node`` in chain order (index 0 is the root)."""
    if index == 0 or _is_leaf(node) or "input" not in node:
        return node
    child = node["input"]
    assert isinstance(child, Mapping)
    return _node_at(child, index - 1)


def _random_leaf(rng: random.Random, vocabulary: FactorVocabulary) -> dict[str, object]:
    return {"field": rng.choice(vocabulary.fields)}


def _random_tree(
    rng: random.Random, vocabulary: FactorVocabulary, depth: int = 0
) -> dict[str, object]:
    if depth >= _TREE_DEPTH_LIMIT or rng.random() < 0.4:
        return _random_leaf(rng, vocabulary)
    return {
        "op": rng.choice(vocabulary.operators),
        "window": rng.choice(vocabulary.windows),
        "input": _random_tree(rng, vocabulary, depth + 1),
    }


def _random_window_shift(
    rng: random.Random, vocabulary: FactorVocabulary, current: int, step: int = 1
) -> int:
    index = vocabulary.windows.index(current)
    offsets = [
        offset
        for offset in range(-step, step + 1)
        if offset != 0 and 0 <= index + offset < len(vocabulary.windows)
    ]
    if not offsets:
        return current
    return vocabulary.windows[index + rng.choice(offsets)]


class FactorCandidateFactory:
    def __init__(
        self,
        vocabulary: FactorVocabulary,
        *,
        seed: int,
        quota: GenerationQuota | None = None,
        mechanism_proposer: MechanismProposer | None = None,
    ) -> None:
        self._vocabulary = vocabulary
        self._rng = random.Random(seed)
        self._quota = quota if quota is not None else GenerationQuota(dict(DEFAULT_RATIOS))
        self._mechanism_proposer = mechanism_proposer
        self._window_step = 1

    def rebind_quota(self, quota: GenerationQuota) -> None:
        """安装探索/利用再平衡后的新配额；不重置已播种的随机流。"""
        self._quota = quota

    def set_window_step(self, step: int) -> None:
        """设置 ``parameter_perturb`` 的自适应窗口步长（窗口索引位移 ±step）。"""
        if step < 1:
            raise ValueError("window step must be positive")
        self._window_step = step

    async def generate(self, count: int, pool: ParentPool) -> list[GeneratedCandidate]:
        """One bounded batch split across strategies by the configured quota."""
        if count < 1:
            raise ValueError("count must be positive")
        candidates: list[GeneratedCandidate] = []
        for strategy in GenerationStrategy:
            batch = self._quota.allocate(count)[strategy]
            if batch == 0:
                continue
            if strategy is GenerationStrategy.LLM_MECHANISM:
                candidates.extend(await self._mechanism_candidates(batch))
            else:
                candidates.extend(self._native_candidates(strategy, batch, pool))
        return candidates

    async def generate_with_strategy(
        self, count: int, strategy: GenerationStrategy, pool: ParentPool
    ) -> list[GeneratedCandidate]:
        """Draw ``count`` candidates from a single strategy, quota aside."""
        if count < 1:
            raise ValueError("count must be positive")
        if strategy is GenerationStrategy.LLM_MECHANISM:
            return await self._mechanism_candidates(count)
        return self._native_candidates(strategy, count, pool)

    def _native_candidates(
        self, strategy: GenerationStrategy, count: int, pool: ParentPool
    ) -> list[GeneratedCandidate]:
        parents = pool.parents()
        if strategy is not GenerationStrategy.RANDOM_EXPLORE and not parents:
            strategy = GenerationStrategy.RANDOM_EXPLORE  # 冷启动：随机探索建立覆盖
        return [GeneratedCandidate(strategy, self._draw(strategy, parents)) for _ in range(count)]

    def _draw(
        self, strategy: GenerationStrategy, parents: tuple[Mapping[str, object], ...]
    ) -> dict[str, object]:
        if strategy is GenerationStrategy.RANDOM_EXPLORE:
            return _random_tree(self._rng, self._vocabulary)
        if strategy is GenerationStrategy.MUTATE:
            return self._mutate(parents)
        if strategy is GenerationStrategy.CROSSOVER:
            return self._crossover(parents)
        return self._perturb(parents)

    def _clone_parent(self, parents: tuple[Mapping[str, object], ...]) -> dict[str, object]:
        return dict(copy.deepcopy(dict(self._rng.choice(parents))))

    def _mutate(self, parents: tuple[Mapping[str, object], ...]) -> dict[str, object]:
        tree = self._clone_parent(parents)
        if _is_leaf(tree):
            tree["field"] = self._other_field(str(tree["field"]))
            return tree
        leaf_slots = [
            node for node in _operator_nodes(tree) if isinstance(node["input"], Mapping) and _is_leaf(node["input"])
        ]
        if not leaf_slots:
            return tree
        node = self._rng.choice(leaf_slots)
        leaf = node["input"]
        assert isinstance(leaf, Mapping)
        node["input"] = {"field": self._other_field(str(leaf["field"]))}
        return tree

    def _crossover(self, parents: tuple[Mapping[str, object], ...]) -> dict[str, object]:
        tree = self._clone_parent(parents)
        donor = copy.deepcopy(dict(self._rng.choice(parents)))
        graft_positions: list[tuple[Mapping[str, object], str] | None] = [None]  # None = 根替换
        for node in _operator_nodes(tree):
            if "input" in node:
                graft_positions.append((node, "input"))
        chosen = self._rng.randrange(len(graft_positions))
        subtree = copy.deepcopy(_node_at(donor, self._rng.randrange(_subtree_count(donor))))
        if chosen == 0:
            assert isinstance(subtree, dict)
            return subtree
        target = graft_positions[chosen]
        assert target is not None
        container, key = target
        assert isinstance(container, dict)
        container[key] = subtree
        return tree

    def _perturb(self, parents: tuple[Mapping[str, object], ...]) -> dict[str, object]:
        tree = self._clone_parent(parents)
        operators = _operator_nodes(tree)
        if not operators:
            return self._mutate(parents)
        node = self._rng.choice(operators)
        node["window"] = _random_window_shift(
            self._rng, self._vocabulary, int(str(node["window"])), self._window_step
        )
        return tree

    def _other_field(self, current: str) -> str:
        options = [field for field in self._vocabulary.fields if field != current]
        return self._rng.choice(options) if options else current

    async def _mechanism_candidates(self, count: int) -> list[GeneratedCandidate]:
        if self._mechanism_proposer is None:
            return self._random_fallback(count)
        try:
            proposed = await self._mechanism_proposer.propose(count)
        except Exception:  # noqa: BLE001 - 降级模型不得阻塞搜索
            return self._random_fallback(count)
        results = [
            GeneratedCandidate(GenerationStrategy.LLM_MECHANISM, dict(proposal))
            for proposal in list(proposed)[:count]
        ]
        shortfall = count - len(results)
        if shortfall:
            results.extend(self._random_fallback(shortfall))
        return results

    def _random_fallback(self, count: int) -> list[GeneratedCandidate]:
        return [
            GeneratedCandidate(
                GenerationStrategy.RANDOM_EXPLORE, _random_tree(self._rng, self._vocabulary)
            )
            for _ in range(count)
        ]
