"""Immutable inputs and explainable outputs for global admission."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CandidateKind(StrEnum):
    STIMULUS = "STIMULUS"
    RUN = "RUN"


class WorkClass(StrEnum):
    REALTIME = "REALTIME"
    RECOVERY = "RECOVERY"
    BACKGROUND = "BACKGROUND"
    DIAGNOSTIC = "DIAGNOSTIC"


class SystemMode(StrEnum):
    NORMAL = "NORMAL"
    REVIEW = "REVIEW"
    DEGRADED = "DEGRADED"
    SAFE = "SAFE"


class AdmissionOutcome(StrEnum):
    ADMIT = "ADMIT"
    DEFER = "DEFER"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class SchedulingCandidate:
    candidate_id: str
    kind: CandidateKind
    work_class: WorkClass
    enqueued_at: datetime
    base_priority: float
    salience: float = 0.0
    goal_urgency: float = 0.0
    deadline: datetime | None = None
    estimated_cost: float = 0.0
    conflict_penalty: float = 0.0
    dependency_penalty: float = 0.0
    dependency_ready: bool = True
    conflict_free: bool = True
    safe_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id must not be empty")
        if self.enqueued_at.tzinfo is None or self.enqueued_at.utcoffset() is None:
            raise ValueError("enqueued_at must be timezone-aware")
        if self.deadline is not None and (
            self.deadline.tzinfo is None or self.deadline.utcoffset() is None
        ):
            raise ValueError("deadline must be timezone-aware")
        for name, value in (
            ("base_priority", self.base_priority),
            ("salience", self.salience),
            ("goal_urgency", self.goal_urgency),
            ("estimated_cost", self.estimated_cost),
            ("conflict_penalty", self.conflict_penalty),
            ("dependency_penalty", self.dependency_penalty),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    mode: SystemMode
    available_slots: int
    remaining_cost: float
    policy_version: str

    def __post_init__(self) -> None:
        if self.available_slots < 0:
            raise ValueError("available_slots must be non-negative")
        if self.remaining_cost < 0:
            raise ValueError("remaining_cost must be non-negative")
        if not self.policy_version:
            raise ValueError("policy_version must not be empty")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    candidate_id: str
    outcome: AdmissionOutcome
    score: float
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    budget: BudgetSnapshot
    next_evaluation_at: datetime | None
