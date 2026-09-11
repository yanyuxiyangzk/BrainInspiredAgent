"""Momentum tracking, adaptive window steps and explore/exploit rebalancing (L-004).

The factor discovery architecture (§3.1/§4) lets the previous round's acceptance
rate, duplicate rate, mechanism coverage and stagnation adjust the next round's
strategy ratios — but no adjustment may leave the Profile Policy bounds. This
module is a pure deterministic library: it never backtests, never writes the
factor library and never owns an event loop. The search state serializes to a
plain dict so checkpoint payloads can persist momentum, adaptive step and the
actual per-strategy quotas, and multi-round replay plus resume-from-snapshot
reproduce the live run round for round.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast

from domain_sdk.factor_generation import (
    DEFAULT_RATIOS,
    FactorCandidateFactory,
    FactorVocabulary,
    GeneratedCandidate,
    GenerationQuota,
    GenerationStrategy,
    MechanismProposer,
    ParentPool,
    candidate_hash,
)

_SNAPSHOT_FORMAT = 1
_WEIGHT_TOLERANCE = 1e-6

EXPLORATION_STRATEGIES: tuple[GenerationStrategy, ...] = (
    GenerationStrategy.RANDOM_EXPLORE,
    GenerationStrategy.LLM_MECHANISM,
)
EXPLOITATION_STRATEGIES: tuple[GenerationStrategy, ...] = (
    GenerationStrategy.MUTATE,
    GenerationStrategy.CROSSOVER,
    GenerationStrategy.PARAMETER_PERTURB,
)

DEFAULT_MIN_RATIOS: Mapping[GenerationStrategy, float] = {
    GenerationStrategy.MUTATE: 0.10,
    GenerationStrategy.CROSSOVER: 0.10,
    GenerationStrategy.PARAMETER_PERTURB: 0.05,
    GenerationStrategy.RANDOM_EXPLORE: 0.05,
    GenerationStrategy.LLM_MECHANISM: 0.05,
}
DEFAULT_MAX_RATIOS: Mapping[GenerationStrategy, float] = {
    GenerationStrategy.MUTATE: 0.40,
    GenerationStrategy.CROSSOVER: 0.40,
    GenerationStrategy.PARAMETER_PERTURB: 0.30,
    GenerationStrategy.RANDOM_EXPLORE: 0.35,
    GenerationStrategy.LLM_MECHANISM: 0.35,
}


@dataclass(frozen=True, slots=True)
class ExplorationPolicy:
    """Profile Policy 上下限：任何一轮反馈都不可能把可调量推出这些边界。"""

    min_ratios: Mapping[GenerationStrategy, float] = field(
        default_factory=lambda: dict(DEFAULT_MIN_RATIOS)
    )
    max_ratios: Mapping[GenerationStrategy, float] = field(
        default_factory=lambda: dict(DEFAULT_MAX_RATIOS)
    )
    max_adjust_step: float = 0.08
    min_exploration_share: float = 0.20
    max_exploration_share: float = 0.55
    momentum_alpha: float = 0.5
    exploitation_threshold: float = 0.30
    duplicate_threshold: float = 0.40
    duplicate_shift: float = 0.05
    stagnation_rounds: int = 3
    min_window_step: int = 1
    max_window_step: int = 3
    tilt_sensitivity: float = 0.5

    def __post_init__(self) -> None:
        if set(self.min_ratios) != set(GenerationStrategy) or set(self.max_ratios) != set(
            GenerationStrategy
        ):
            raise ValueError("policy bounds must cover all five strategies")
        min_sum, max_sum = 0.0, 0.0
        for strategy in GenerationStrategy:
            low, high = self.min_ratios[strategy], self.max_ratios[strategy]
            if not 0.0 <= low <= high <= 1.0:
                raise ValueError("min ratio must not exceed max ratio")
            min_sum += low
            max_sum += high
        if min_sum > 1.0 + 1e-9 or max_sum < 1.0 - 1e-9:
            raise ValueError("sum of policy bounds must bracket 1")
        explore_min = sum(self.min_ratios[s] for s in EXPLORATION_STRATEGIES)
        explore_max = sum(self.max_ratios[s] for s in EXPLORATION_STRATEGIES)
        if not explore_min <= self.min_exploration_share <= self.max_exploration_share <= explore_max:
            raise ValueError("exploration share window is infeasible for the exploration group")
        exploit_min = sum(self.min_ratios[s] for s in EXPLOITATION_STRATEGIES)
        exploit_max = sum(self.max_ratios[s] for s in EXPLOITATION_STRATEGIES)
        if exploit_min > 1.0 - self.max_exploration_share + 1e-9 or (
            1.0 - self.min_exploration_share > exploit_max + 1e-9
        ):
            raise ValueError("exploitation share window is infeasible for the exploitation group")
        if not 0.0 < self.momentum_alpha <= 1.0:
            raise ValueError("momentum alpha must stay within (0, 1]")
        if self.max_adjust_step <= 0.0 or self.duplicate_shift < 0.0:
            raise ValueError("adjust steps must be non-negative")
        if not 0.0 <= self.exploitation_threshold <= 1.0:
            raise ValueError("exploitation threshold must stay within [0, 1]")
        if not 0.0 <= self.duplicate_threshold <= 1.0:
            raise ValueError("duplicate threshold must stay within [0, 1]")
        if self.stagnation_rounds < 1:
            raise ValueError("stagnation rounds must be positive")
        if not 1 <= self.min_window_step <= self.max_window_step:
            raise ValueError("window step bounds are invalid")
        if not 0.0 < self.tilt_sensitivity <= 1.0:
            raise ValueError("tilt sensitivity must stay within (0, 1]")


def _base_group_weights(ratios: Mapping[GenerationStrategy, float]) -> dict[GenerationStrategy, float]:
    weights: dict[GenerationStrategy, float] = {}
    for group in (EXPLORATION_STRATEGIES, EXPLOITATION_STRATEGIES):
        total = sum(ratios[s] for s in group)
        for strategy in group:
            weights[strategy] = ratios[strategy] / total if total > 0 else 1.0 / len(group)
    return weights


def _validate_group_weights(weights: Mapping[str, object]) -> dict[GenerationStrategy, float]:
    expected = {strategy.value for strategy in GenerationStrategy}
    if set(weights) != expected:
        raise ValueError("group weights must cover all five strategies")
    parsed: dict[GenerationStrategy, float] = {}
    for strategy in GenerationStrategy:
        value = weights[strategy.value]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("group weights must be positive numbers")
        parsed[strategy] = float(value)
    for group in (EXPLORATION_STRATEGIES, EXPLOITATION_STRATEGIES):
        total = sum(parsed[s] for s in group)
        if abs(total - 1.0) > _WEIGHT_TOLERANCE:
            raise ValueError("group weights must sum to 1 within each group")
    return parsed


@dataclass(frozen=True, slots=True)
class FactorSearchState:
    """可序列化的搜索调节状态：动量、探索份额、自适应步长与组内权重。"""

    rounds: int
    momentum: float | None
    exploration_share: float
    window_step: int
    zero_accept_streak: int
    group_weights: Mapping[GenerationStrategy, float] = field(
        default_factory=lambda: _base_group_weights(DEFAULT_RATIOS)
    )

    def __post_init__(self) -> None:
        if self.rounds < 0 or self.zero_accept_streak < 0:
            raise ValueError("round counters must be non-negative")
        if self.momentum is not None and not 0.0 <= self.momentum <= 1.0:
            raise ValueError("momentum must stay within [0, 1]")
        if not 0.0 <= self.exploration_share <= 1.0:
            raise ValueError("exploration share must stay within [0, 1]")
        if self.window_step < 1:
            raise ValueError("window step must be positive")
        if set(self.group_weights) != set(GenerationStrategy):
            raise ValueError("group weights must cover all five strategies")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _SNAPSHOT_FORMAT,
            "rounds": self.rounds,
            "momentum": self.momentum,
            "exploration_share": self.exploration_share,
            "window_step": self.window_step,
            "zero_accept_streak": self.zero_accept_streak,
            "group_weights": {
                strategy.value: self.group_weights[strategy] for strategy in GenerationStrategy
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FactorSearchState:
        if payload.get("format") != _SNAPSHOT_FORMAT:
            raise ValueError("unsupported search state snapshot format")
        rounds = payload.get("rounds")
        streak = payload.get("zero_accept_streak")
        step = payload.get("window_step")
        for name, value in (("rounds", rounds), ("zero_accept_streak", streak), ("window_step", step)):
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if name == "window_step" else 0):
                raise ValueError(f"invalid {name} in search state snapshot")
        momentum = payload.get("momentum")
        if momentum is not None and (
            isinstance(momentum, bool) or not isinstance(momentum, (int, float))
            or not 0.0 <= float(momentum) <= 1.0
        ):
            raise ValueError("momentum must stay within [0, 1]")
        share = payload.get("exploration_share")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0.0 <= float(share) <= 1.0:
            raise ValueError("exploration share must stay within [0, 1]")
        raw_weights = payload.get("group_weights")
        if not isinstance(raw_weights, Mapping):
            raise TypeError("group weights snapshot is required")
        return cls(
            rounds=cast(int, rounds),
            momentum=None if momentum is None else float(momentum),
            exploration_share=float(share),
            window_step=cast(int, step),
            zero_accept_streak=cast(int, streak),
            group_weights=_validate_group_weights(cast(Mapping[str, object], dict(raw_weights))),
        )


@dataclass(frozen=True, slots=True)
class RoundFeedback:
    """上一轮的回测事实：逐策略回测/入库计数、重复候选数与生成总数。"""

    tested_by_strategy: Mapping[GenerationStrategy, int]
    accepted_by_strategy: Mapping[GenerationStrategy, int]
    duplicates: int
    generated: int

    def __post_init__(self) -> None:
        if any(count < 0 for count in self.tested_by_strategy.values()) or any(
            count < 0 for count in self.accepted_by_strategy.values()
        ):
            raise ValueError("feedback counts must be non-negative")
        for strategy, accepted in self.accepted_by_strategy.items():
            if accepted > self.tested_by_strategy.get(strategy, 0):
                raise ValueError("accepted candidates cannot exceed tested candidates")
        if self.duplicates < 0 or self.generated < 0 or self.duplicates > self.generated:
            raise ValueError("duplicates must stay within generated candidates")

    @property
    def total_tested(self) -> int:
        return sum(self.tested_by_strategy.values())

    @property
    def total_accepted(self) -> int:
        return sum(self.accepted_by_strategy.values())

    @property
    def acceptance_rate(self) -> float:
        tested = self.total_tested
        return self.total_accepted / tested if tested else 0.0

    @property
    def duplicate_rate(self) -> float:
        return self.duplicates / self.generated if self.generated else 0.0

    @classmethod
    def from_round(
        cls,
        generated: GeneratedRound,
        tested: Iterable[str],
        accepted: Iterable[str],
    ) -> RoundFeedback:
        """按候选哈希把本轮回测/入库结果归位到生成策略上。"""
        tested_set = frozenset(tested)
        accepted_set = frozenset(accepted)
        tested_counts: dict[GenerationStrategy, int] = {}
        accepted_counts: dict[GenerationStrategy, int] = {}
        for item in generated.candidates:
            digest = candidate_hash(item.definition)
            if digest in tested_set:
                tested_counts[item.strategy] = tested_counts.get(item.strategy, 0) + 1
            if digest in accepted_set:
                accepted_counts[item.strategy] = accepted_counts.get(item.strategy, 0) + 1
        return cls(
            tested_by_strategy=tested_counts,
            accepted_by_strategy=accepted_counts,
            duplicates=generated.duplicates,
            generated=len(generated.candidates),
        )


@dataclass(frozen=True, slots=True)
class GeneratedRound:
    """一轮生成的产物：候选、实际逐策略配额、步长与重复数（供 checkpoint 记录）。"""

    candidates: tuple[GeneratedCandidate, ...]
    quota: Mapping[GenerationStrategy, int]
    window_step: int
    duplicates: int

    def __post_init__(self) -> None:
        if self.window_step < 1:
            raise ValueError("window step must be positive")
        if self.duplicates < 0 or any(count < 0 for count in self.quota.values()):
            raise ValueError("round counts must be non-negative")


def _bounded_proportional(
    total: float,
    weights: Mapping[GenerationStrategy, float],
    bounds: Mapping[GenerationStrategy, tuple[float, float]],
) -> dict[GenerationStrategy, float]:
    """按权重把 ``total`` 分到组内策略；逐策略夹紧到边界后迭代分摊残差。"""
    if not weights:
        return {}
    allocated: dict[GenerationStrategy, float] = {}
    weight_sum = sum(weights.values())
    for strategy, weight in weights.items():
        allocated[strategy] = min(max(total * weight / weight_sum, bounds[strategy][0]), bounds[strategy][1])
    residual = total - sum(allocated.values())
    free = [strategy for strategy in weights if abs(residual) > 1e-12]
    for _ in range(len(weights) + 1):
        if abs(residual) <= 1e-12 or not free:
            break
        free_weight = sum(weights[s] for s in free)
        still_free: list[GenerationStrategy] = []
        progress = 0.0
        for strategy in free:
            delta = residual * weights[strategy] / free_weight
            low, high = bounds[strategy]
            room = (high - allocated[strategy]) if delta > 0 else (low - allocated[strategy])
            applied = delta if abs(delta) <= abs(room) else room
            allocated[strategy] += applied
            progress += applied
            if abs(abs(delta) - abs(room)) > 1e-12 and abs(room) > 1e-12:
                still_free.append(strategy)
        residual -= progress
        free = still_free
    if abs(residual) > 1e-9:
        raise ValueError("quota allocation failed to converge within policy bounds")
    return allocated


def _tilt_group_weights(
    weights: Mapping[GenerationStrategy, float],
    feedback: RoundFeedback,
    sensitivity: float,
) -> dict[GenerationStrategy, float]:
    """机制覆盖倾斜：组内入库表现好的策略获得更高组内权重；未回测策略保持原权重。"""
    scores = {
        strategy: feedback.accepted_by_strategy.get(strategy, 0) / count
        for strategy, count in feedback.tested_by_strategy.items()
        if count > 0
    }
    if not scores:
        return dict(weights)
    tilted = dict(weights)
    for group in (EXPLORATION_STRATEGIES, EXPLOITATION_STRATEGIES):
        observed = [s for s in group if s in scores]
        if not observed:
            continue
        mean = sum(scores[s] for s in observed) / len(observed)
        adjusted = {
            s: weights[s] * (1.0 + sensitivity * (scores[s] - mean)) if s in scores else weights[s]
            for s in group
        }
        total = sum(adjusted.values())
        if total <= 0:
            continue  # 防御：sensitivity<=1 时不可达，保持原权重
        tilted.update({s: value / total for s, value in adjusted.items()})
    return tilted


class FactorSearchGovernor:
    """把上一轮反馈确定性地转成下一轮配额与步长；有界、可解释、可序列化。"""

    def __init__(
        self,
        policy: ExplorationPolicy | None = None,
        *,
        base_ratios: Mapping[GenerationStrategy, float] | None = None,
    ) -> None:
        self._policy = policy if policy is not None else ExplorationPolicy()
        base = dict(DEFAULT_RATIOS) if base_ratios is None else dict(base_ratios)
        if set(base) != set(GenerationStrategy):
            raise ValueError("base ratios must cover all five strategies")
        for strategy, ratio in base.items():
            if not self._policy.min_ratios[strategy] <= ratio <= self._policy.max_ratios[strategy]:
                raise ValueError("base ratio outside policy bounds")
        self._state = FactorSearchState(
            rounds=0,
            momentum=None,
            exploration_share=sum(base[s] for s in EXPLORATION_STRATEGIES),
            window_step=self._policy.min_window_step,
            zero_accept_streak=0,
            group_weights=_base_group_weights(base),
        )

    @property
    def policy(self) -> ExplorationPolicy:
        return self._policy

    @property
    def state(self) -> FactorSearchState:
        return self._state

    def current_quota(self) -> GenerationQuota:
        """当前反馈状态下的五策略配额；任何策略都不越出 Profile Policy 上下限。"""
        policy = self._policy
        weights = self._state.group_weights
        bounds = {
            strategy: (policy.min_ratios[strategy], policy.max_ratios[strategy])
            for strategy in GenerationStrategy
        }
        ratios = _bounded_proportional(
            self._state.exploration_share,
            {s: weights[s] for s in EXPLORATION_STRATEGIES},
            bounds,
        )
        exploit_total = 1.0 - sum(ratios.values())
        ratios.update(
            _bounded_proportional(
                exploit_total,
                {s: weights[s] for s in EXPLOITATION_STRATEGIES},
                bounds,
            )
        )
        return GenerationQuota(ratios)

    def observe(self, feedback: RoundFeedback) -> FactorSearchState:
        """吸收一轮回测事实：更新动量、停滞计数、探索份额、步长与组内权重。"""
        policy = self._policy
        state = self._state
        tested = feedback.total_tested
        accepted = feedback.total_accepted
        momentum = state.momentum
        streak = state.zero_accept_streak
        rate = 0.0
        if tested > 0:
            rate = accepted / tested
            momentum = (
                rate
                if momentum is None
                else policy.momentum_alpha * rate + (1.0 - policy.momentum_alpha) * momentum
            )
            streak = 0 if accepted > 0 else streak + 1
        shift = 0.0
        if tested > 0 and rate >= policy.exploitation_threshold:
            shift -= policy.max_adjust_step  # 入库率达标：收敛期，转向利用
        if tested > 0 and streak >= policy.stagnation_rounds:
            shift += policy.max_adjust_step  # 连续零入库：停滞，转向探索
        if feedback.duplicate_rate >= policy.duplicate_threshold:
            shift += policy.duplicate_shift  # 重复率过高：当前区域耗尽，转向探索
        shift = max(-policy.max_adjust_step, min(policy.max_adjust_step, shift))
        share = min(
            max(state.exploration_share + shift, policy.min_exploration_share),
            policy.max_exploration_share,
        )
        step = state.window_step
        if momentum is not None:
            previous = state.momentum
            if momentum >= policy.exploitation_threshold and (
                previous is None or momentum >= previous
            ):
                step = policy.min_window_step  # 动量向好：细粒度利用
            elif streak >= policy.stagnation_rounds or (
                previous is not None and momentum < previous
            ):
                step = min(step + 1, policy.max_window_step)  # 动量回落：放大探索步长
        weights = _tilt_group_weights(state.group_weights, feedback, policy.tilt_sensitivity)
        self._state = FactorSearchState(
            rounds=state.rounds + 1,
            momentum=momentum,
            exploration_share=share,
            window_step=step,
            zero_accept_streak=streak,
            group_weights=weights,
        )
        return self._state

    def snapshot(self) -> dict[str, object]:
        return self._state.to_dict()

    @classmethod
    def restore(
        cls,
        payload: Mapping[str, object],
        policy: ExplorationPolicy | None = None,
        *,
        base_ratios: Mapping[GenerationStrategy, float] | None = None,
    ) -> FactorSearchGovernor:
        """从 checkpoint 恢复调节状态；恢复值必须落在新 Profile Policy 窗口内。"""
        effective = policy if policy is not None else ExplorationPolicy()
        state = FactorSearchState.from_dict(payload)
        if not (
            effective.min_exploration_share - 1e-9
            <= state.exploration_share
            <= effective.max_exploration_share + 1e-9
        ):
            raise ValueError("restored exploration share is outside the policy window")
        if not effective.min_window_step <= state.window_step <= effective.max_window_step:
            raise ValueError("restored window step is outside the policy bounds")
        governor = cls(effective, base_ratios=base_ratios)
        governor._state = state
        return governor


def _round_seed(base_seed: int, round_index: int) -> int:
    """每轮派生独立种子：断点续跑只需 (seed, 已完成轮数) 即可复现随机流。"""
    payload = f"{base_seed}:{round_index}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


class AdaptiveFactorSearch:
    """按轮驱动纯生成工厂：每轮换配额与步长，回合后吸收反馈；同 seed 逐轮可复现。"""

    def __init__(
        self,
        vocabulary: FactorVocabulary,
        *,
        seed: int,
        policy: ExplorationPolicy | None = None,
        base_ratios: Mapping[GenerationStrategy, float] | None = None,
        mechanism_proposer: MechanismProposer | None = None,
    ) -> None:
        self._vocabulary = vocabulary
        self._seed = seed
        self._mechanism_proposer = mechanism_proposer
        self._governor = FactorSearchGovernor(policy, base_ratios=base_ratios)

    @property
    def state(self) -> FactorSearchState:
        return self._governor.state

    @property
    def policy(self) -> ExplorationPolicy:
        return self._governor.policy

    def current_quota(self) -> GenerationQuota:
        return self._governor.current_quota()

    async def run_round(
        self,
        count: int,
        pool: ParentPool,
        *,
        seen_hashes: Iterable[str] = (),
    ) -> GeneratedRound:
        """生成一轮候选；候选批次、实际配额、步长与重复数一并返回。

        ``seen_hashes`` 是历史轮次已回测候选的哈希（checkpoint 的已测试集合）：
        与之重复同样计入重复率，作为区域耗尽信号。
        """
        if count < 1:
            raise ValueError("count must be positive")
        quota = self._governor.current_quota()
        step = self._governor.state.window_step
        factory = FactorCandidateFactory(
            self._vocabulary,
            seed=_round_seed(self._seed, self._governor.state.rounds),
            quota=quota,
            mechanism_proposer=self._mechanism_proposer,
        )
        factory.set_window_step(step)
        candidates = await factory.generate(count, pool)
        pool_hashes = {candidate_hash(parent) for parent in pool.parents()}
        history = frozenset(seen_hashes)
        seen: set[str] = set()
        duplicates = 0
        counts = {strategy: 0 for strategy in GenerationStrategy}
        for item in candidates:
            counts[item.strategy] += 1
            digest = candidate_hash(item.definition)
            if digest in seen or digest in pool_hashes or digest in history:
                duplicates += 1
            seen.add(digest)
        return GeneratedRound(
            candidates=tuple(candidates), quota=counts, window_step=step, duplicates=duplicates
        )

    def observe(self, feedback: RoundFeedback) -> FactorSearchState:
        return self._governor.observe(feedback)

    def snapshot(self) -> dict[str, object]:
        payload = dict(self._governor.snapshot())
        payload["seed"] = self._seed
        payload["mechanism_proposer"] = self._mechanism_proposer is not None
        return payload

    @classmethod
    def restore(
        cls,
        payload: Mapping[str, object],
        vocabulary: FactorVocabulary,
        *,
        policy: ExplorationPolicy | None = None,
        base_ratios: Mapping[GenerationStrategy, float] | None = None,
        mechanism_proposer: MechanismProposer | None = None,
    ) -> AdaptiveFactorSearch:
        """从快照恢复搜索；后续轮次与全程推进逐轮一致（多轮回放契约）。

        快照若来自带机制提案器的搜索，恢复时必须重新注入等价提案器，
        否则拒绝恢复——静默降级为随机探索会让回放悄悄漂移。
        """
        seed = payload.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("snapshot seed is required")
        if payload.get("mechanism_proposer") and mechanism_proposer is None:
            raise ValueError("snapshot was created with a mechanism proposer; inject it on restore")
        search = cls(
            vocabulary,
            seed=seed,
            policy=policy,
            base_ratios=base_ratios,
            mechanism_proposer=mechanism_proposer,
        )
        search._governor = FactorSearchGovernor.restore(
            payload, policy, base_ratios=base_ratios
        )
        return search
