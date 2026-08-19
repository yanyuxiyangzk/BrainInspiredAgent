"""Auditable scheduling policy with priority aging and hard safety gates."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from active_agent_platform.scheduling.models import (
    AdmissionDecision,
    AdmissionOutcome,
    BudgetSnapshot,
    CandidateKind,
    SchedulingCandidate,
    SystemMode,
    WorkClass,
)
from brain_kernel.ports import Clock


class CorticalSchedulingPolicy(Protocol):
    def evaluate(
        self,
        candidates: tuple[SchedulingCandidate, ...],
        budget: BudgetSnapshot,
    ) -> tuple[AdmissionDecision, ...]: ...


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    version: str = "deterministic-1.0"
    admission_threshold: float = 80.0
    deadline_horizon_seconds: float = 300.0
    deadline_bonus: float = 30.0
    recovery_bonus: float = 40.0
    realtime_bonus: float = 15.0
    review_background_bonus: float = 20.0
    aging_interval_seconds: float = 60.0
    aging_step: float = 10.0
    maximum_aging_bonus: float = 100.0
    reevaluation_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("version must not be empty")
        for name, value in (
            ("deadline_horizon_seconds", self.deadline_horizon_seconds),
            ("aging_interval_seconds", self.aging_interval_seconds),
            ("reevaluation_seconds", self.reevaluation_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        for name, value in (
            ("admission_threshold", self.admission_threshold),
            ("deadline_bonus", self.deadline_bonus),
            ("recovery_bonus", self.recovery_bonus),
            ("realtime_bonus", self.realtime_bonus),
            ("review_background_bonus", self.review_background_bonus),
            ("aging_step", self.aging_step),
            ("maximum_aging_bonus", self.maximum_aging_bonus),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")


class DeterministicCorticalPolicy:
    def __init__(self, clock: Clock, config: PolicyConfig | None = None) -> None:
        self._clock = clock
        self._config = config or PolicyConfig()

    def evaluate(
        self,
        candidates: tuple[SchedulingCandidate, ...],
        budget: BudgetSnapshot,
    ) -> tuple[AdmissionDecision, ...]:
        if budget.policy_version != self._config.version:
            raise ValueError("budget policy_version does not match active policy")
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")

        now = self._clock.now()
        scored = sorted(
            ((self._score(candidate, budget, now), candidate) for candidate in candidates),
            key=lambda item: (-item[0], item[1].enqueued_at, item[1].candidate_id),
        )
        slots = budget.available_slots
        remaining_cost = budget.remaining_cost
        decisions: dict[str, AdmissionDecision] = {}
        for score, candidate in scored:
            outcome, rules, reasons = self._hard_decision(candidate, budget, now)
            if outcome is None:
                if score < self._config.admission_threshold:
                    outcome = AdmissionOutcome.DEFER
                    rules.append("SCORE_BELOW_THRESHOLD")
                    reasons.append("score is below the active admission threshold")
                elif slots == 0:
                    outcome = AdmissionOutcome.DEFER
                    rules.append("NO_CONCURRENCY_SLOT")
                    reasons.append("no global concurrency slot is available")
                elif candidate.estimated_cost > remaining_cost:
                    outcome = AdmissionOutcome.DEFER
                    rules.append("COST_BUDGET_UNAVAILABLE")
                    reasons.append("remaining cost budget is insufficient")
                else:
                    outcome = AdmissionOutcome.ADMIT
                    rules.append("SCORE_AND_BUDGET_ADMIT")
                    reasons.append("score and global budget permit admission")
                    slots -= 1
                    remaining_cost -= candidate.estimated_cost

            next_evaluation = (
                now + timedelta(seconds=self._config.reevaluation_seconds)
                if outcome is AdmissionOutcome.DEFER
                else None
            )
            decisions[candidate.candidate_id] = AdmissionDecision(
                candidate_id=candidate.candidate_id,
                outcome=outcome,
                score=round(score, 6),
                rule_ids=tuple(rules),
                reasons=tuple(reasons),
                budget=BudgetSnapshot(
                    mode=budget.mode,
                    available_slots=slots,
                    remaining_cost=round(remaining_cost, 6),
                    policy_version=budget.policy_version,
                ),
                next_evaluation_at=next_evaluation,
            )
        return tuple(decisions[candidate.candidate_id] for candidate in candidates)

    def rank_stimuli(
        self, candidates: tuple[SchedulingCandidate, ...], budget: BudgetSnapshot
    ) -> tuple[AdmissionDecision, ...]:
        return self.evaluate(
            tuple(candidate for candidate in candidates if candidate.kind is CandidateKind.STIMULUS),
            budget,
        )

    def rank_runs(
        self, candidates: tuple[SchedulingCandidate, ...], budget: BudgetSnapshot
    ) -> tuple[AdmissionDecision, ...]:
        return self.evaluate(
            tuple(candidate for candidate in candidates if candidate.kind is CandidateKind.RUN),
            budget,
        )

    def _score(
        self, candidate: SchedulingCandidate, budget: BudgetSnapshot, now: datetime
    ) -> float:
        waited_seconds = max(0.0, (now - candidate.enqueued_at).total_seconds())
        aging_bonus = min(
            (waited_seconds // self._config.aging_interval_seconds) * self._config.aging_step,
            self._config.maximum_aging_bonus,
        )
        deadline_urgency = 0.0
        if candidate.deadline is not None:
            remaining = max(0.0, (candidate.deadline - now).total_seconds())
            deadline_urgency = self._config.deadline_bonus * max(
                0.0, 1.0 - remaining / self._config.deadline_horizon_seconds
            )
        class_bonus = 0.0
        if candidate.work_class is WorkClass.RECOVERY:
            class_bonus += self._config.recovery_bonus
        if candidate.work_class is WorkClass.REALTIME:
            class_bonus += self._config.realtime_bonus
        if budget.mode is SystemMode.REVIEW and candidate.work_class is WorkClass.BACKGROUND:
            class_bonus += self._config.review_background_bonus
        return (
            candidate.base_priority
            + candidate.salience
            + candidate.goal_urgency
            + deadline_urgency
            + class_bonus
            + aging_bonus
            - candidate.estimated_cost
            - candidate.conflict_penalty
            - candidate.dependency_penalty
        )

    @staticmethod
    def _hard_decision(
        candidate: SchedulingCandidate,
        budget: BudgetSnapshot,
        now: datetime,
    ) -> tuple[AdmissionOutcome | None, list[str], list[str]]:
        if candidate.deadline is not None and candidate.deadline <= now:
            return AdmissionOutcome.REJECT, ["DEADLINE_EXPIRED"], ["candidate deadline has expired"]
        if budget.mode is SystemMode.SAFE and not candidate.safe_allowed:
            return AdmissionOutcome.REJECT, ["SAFE_MODE_DENY"], ["candidate is not allowed in SAFE mode"]
        if not candidate.dependency_ready:
            return (
                AdmissionOutcome.DEFER,
                ["DEPENDENCY_NOT_READY"],
                ["a required dependency is not ready"],
            )
        if not candidate.conflict_free:
            return AdmissionOutcome.DEFER, ["CONFLICT_ACTIVE"], ["a conflicting run is active"]
        return None, ["HARD_GATES_PASSED"], ["all hard admission gates passed"]
