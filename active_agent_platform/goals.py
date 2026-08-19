"""Deterministic fixed-goal policy for cognition-cycle coordination."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from re import fullmatch
from types import MappingProxyType

from brain_kernel.ports import Clock


class ConditionOperator(StrEnum):
    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"


class GoalStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class GoalBudget:
    max_tokens: int
    max_cost_minor: int
    currency: str
    max_duration_seconds: int

    def __post_init__(self) -> None:
        if self.max_tokens < 0 or self.max_cost_minor < 0:
            raise ValueError("token and cost budgets must be non-negative")
        if not fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be a three-letter uppercase code")
        if not 1 <= self.max_duration_seconds <= 86_400:
            raise ValueError("max_duration_seconds must be between 1 and 86400")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_minor": self.max_cost_minor,
            "currency": self.currency,
            "max_duration_seconds": self.max_duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class CompletionCondition:
    condition_id: str
    path: str
    operator: ConditionOperator
    expected: object

    def __post_init__(self) -> None:
        if not self.condition_id or not self.path or any(not part for part in self.path.split(".")):
            raise ValueError("condition_id and path must not be empty")
        if isinstance(self.expected, float) and not isfinite(self.expected):
            raise ValueError("condition expected value must be finite")

    def matches(self, context: Mapping[str, object]) -> bool:
        found, actual = _resolve(context, self.path)
        if not found:
            return False
        if self.operator is ConditionOperator.EQ:
            return actual == self.expected
        if self.operator is ConditionOperator.NE:
            return actual != self.expected
        if isinstance(actual, bool) or isinstance(self.expected, bool):
            return False
        if not isinstance(actual, int | float) or not isinstance(self.expected, int | float):
            return False
        left, right = float(actual), float(self.expected)
        if not isfinite(left) or not isfinite(right):
            return False
        if self.operator is ConditionOperator.GT:
            return left > right
        if self.operator is ConditionOperator.GTE:
            return left >= right
        if self.operator is ConditionOperator.LT:
            return left < right
        return left <= right


@dataclass(frozen=True, slots=True)
class GoalDefinition:
    goal_id: str
    version: int
    priority: int
    conflict_domain: str
    deadline: datetime | None
    budget: GoalBudget
    completion_conditions: tuple[CompletionCondition, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if not fullmatch(r"[a-z][a-z0-9_.-]{0,127}", self.goal_id):
            raise ValueError("goal_id has an invalid format")
        if self.version < 1:
            raise ValueError("goal version must be positive")
        if not 0 <= self.priority <= 100:
            raise ValueError("goal priority must be between 0 and 100")
        if not self.conflict_domain:
            raise ValueError("conflict_domain must not be empty")
        if self.deadline is not None:
            if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
                raise ValueError("goal deadline must be timezone-aware")
            object.__setattr__(self, "deadline", self.deadline.astimezone(UTC))
        ids = [condition.condition_id for condition in self.completion_conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("completion condition IDs must be unique within a goal")


@dataclass(frozen=True, slots=True)
class GoalEvaluation:
    goal: GoalDefinition
    status: GoalStatus
    evaluated_at: datetime
    unmet_condition_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    version: int
    created_at: datetime
    evaluations: Mapping[str, GoalEvaluation]
    selected_goal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evaluations", MappingProxyType(dict(self.evaluations)))

    def get(self, goal_id: str) -> GoalEvaluation | None:
        return self.evaluations.get(goal_id)


class GoalPolicy:
    """Evaluate fixed goals and select at most one goal per conflict domain."""

    def __init__(self, clock: Clock, goals: tuple[GoalDefinition, ...]) -> None:
        ids = [goal.goal_id for goal in goals]
        if len(ids) != len(set(ids)):
            raise ValueError("goal IDs must be unique")
        self._clock = clock
        self._goals = goals
        now = _aware_utc(clock.now())
        self._snapshot = GoalSnapshot(0, now, {}, ())

    @property
    def snapshot(self) -> GoalSnapshot:
        return self._snapshot

    def evaluate(self, context: Mapping[str, object]) -> GoalSnapshot:
        now = _aware_utc(self._clock.now())
        evaluations = {goal.goal_id: self._evaluate_goal(goal, context, now) for goal in self._goals}
        selected = self._select(evaluations)
        self._snapshot = GoalSnapshot(
            self._snapshot.version + 1,
            now,
            evaluations,
            tuple(item.goal.goal_id for item in selected),
        )
        return self._snapshot

    @staticmethod
    def _evaluate_goal(
        goal: GoalDefinition, context: Mapping[str, object], now: datetime
    ) -> GoalEvaluation:
        if not goal.enabled:
            return GoalEvaluation(goal, GoalStatus.DISABLED, now, (), "goal is disabled")
        if goal.deadline is not None and now >= goal.deadline:
            return GoalEvaluation(goal, GoalStatus.EXPIRED, now, (), "goal deadline reached")
        unmet = tuple(
            condition.condition_id
            for condition in goal.completion_conditions
            if not condition.matches(context)
        )
        if goal.completion_conditions and not unmet:
            return GoalEvaluation(goal, GoalStatus.COMPLETED, now, (), "all completion conditions met")
        return GoalEvaluation(goal, GoalStatus.AVAILABLE, now, unmet, "goal remains actionable")

    @staticmethod
    def _select(evaluations: Mapping[str, GoalEvaluation]) -> tuple[GoalEvaluation, ...]:
        candidates = sorted(
            (item for item in evaluations.values() if item.status is GoalStatus.AVAILABLE),
            key=lambda item: (
                -item.goal.priority,
                item.goal.deadline or datetime.max.replace(tzinfo=UTC),
                item.goal.goal_id,
            ),
        )
        selected: list[GoalEvaluation] = []
        occupied: set[str] = set()
        for candidate in candidates:
            if candidate.goal.conflict_domain in occupied:
                continue
            occupied.add(candidate.goal.conflict_domain)
            selected.append(candidate)
        return tuple(selected)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock.now() must be timezone-aware")
    return value.astimezone(UTC)


def _resolve(context: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = context
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current
