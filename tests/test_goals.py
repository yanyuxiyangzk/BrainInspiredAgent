from datetime import UTC, datetime, timedelta

import pytest

from active_agent_platform import (
    CompletionCondition,
    ConditionOperator,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    GoalStatus,
)
from active_agent_platform.foundation import FakeClock

NOW = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
BUDGET = GoalBudget(2_000, 100, "USD", 60)


class NaiveClock:
    def now(self) -> datetime:
        return NOW.replace(tzinfo=None)

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


def goal(
    goal_id: str,
    *,
    priority: int = 50,
    domain: str = "analysis",
    deadline: datetime | None = None,
    conditions: tuple[CompletionCondition, ...] = (),
    enabled: bool = True,
) -> GoalDefinition:
    return GoalDefinition(goal_id, 1, priority, domain, deadline, BUDGET, conditions, enabled)


def test_goal_policy_evaluates_conditions_and_returns_immutable_versioned_snapshot() -> None:
    clock = FakeClock(NOW)
    condition = CompletionCondition("summary.ready", "result.ready", ConditionOperator.EQ, True)
    policy = GoalPolicy(clock, (goal("produce.summary", conditions=(condition,)),))

    first = policy.evaluate({"result": {"ready": False}})
    evaluation = first.get("produce.summary")
    assert first.version == 1 and first.selected_goal_ids == ("produce.summary",)
    assert evaluation is not None and evaluation.status is GoalStatus.AVAILABLE
    assert evaluation.unmet_condition_ids == ("summary.ready",)
    with pytest.raises(TypeError):
        first.evaluations["other"] = evaluation  # type: ignore[index]

    second = policy.evaluate({"result": {"ready": True}})
    assert second.version == 2 and second.selected_goal_ids == ()
    assert second.get("produce.summary").status is GoalStatus.COMPLETED  # type: ignore[union-attr]
    assert first.get("produce.summary").status is GoalStatus.AVAILABLE  # type: ignore[union-attr]


def test_goal_policy_enforces_deadline_and_disabled_status() -> None:
    clock = FakeClock(NOW)
    policy = GoalPolicy(
        clock,
        (
            goal("expired.goal", deadline=NOW),
            goal("disabled.goal", enabled=False),
            goal("future.goal", deadline=NOW + timedelta(seconds=1)),
        ),
    )
    snapshot = policy.evaluate({})
    assert snapshot.get("expired.goal").status is GoalStatus.EXPIRED  # type: ignore[union-attr]
    assert snapshot.get("disabled.goal").status is GoalStatus.DISABLED  # type: ignore[union-attr]
    assert snapshot.selected_goal_ids == ("future.goal",)
    clock.advance(1)
    assert policy.evaluate({}).selected_goal_ids == ()


def test_goal_policy_selects_deterministically_by_conflict_domain() -> None:
    policy = GoalPolicy(
        FakeClock(NOW),
        (
            goal("same.low", priority=20, domain="shared"),
            goal("same.high.later", priority=90, domain="shared", deadline=NOW + timedelta(hours=2)),
            goal("same.high.sooner", priority=90, domain="shared", deadline=NOW + timedelta(hours=1)),
            goal("independent", priority=10, domain="other"),
        ),
    )
    assert policy.evaluate({}).selected_goal_ids == ("same.high.sooner", "independent")


@pytest.mark.parametrize(
    ("operator", "expected", "matches"),
    [
        (ConditionOperator.EQ, 10, True),
        (ConditionOperator.NE, 9, True),
        (ConditionOperator.GT, 9, True),
        (ConditionOperator.GTE, 10, True),
        (ConditionOperator.LT, 11, True),
        (ConditionOperator.LTE, 10, True),
        (ConditionOperator.GT, 10, False),
    ],
)
def test_completion_condition_operators(
    operator: ConditionOperator, expected: object, matches: bool
) -> None:
    condition = CompletionCondition("condition", "nested.value", operator, expected)
    assert condition.matches({"nested": {"value": 10}}) is matches
    assert not condition.matches({})


def test_goal_budget_matches_plan_budget_shape() -> None:
    assert BUDGET.to_dict() == {
        "max_tokens": 2_000,
        "max_cost_minor": 100,
        "currency": "USD",
        "max_duration_seconds": 60,
    }


def test_goal_configuration_validation() -> None:
    with pytest.raises(ValueError):
        GoalBudget(-1, 0, "USD", 1)
    with pytest.raises(ValueError):
        GoalBudget(0, 0, "usd", 1)
    with pytest.raises(ValueError):
        GoalBudget(0, 0, "USD", 0)
    with pytest.raises(ValueError):
        goal("INVALID")
    with pytest.raises(ValueError):
        GoalDefinition("valid", 0, 1, "domain", None, BUDGET)
    with pytest.raises(ValueError):
        goal("valid", priority=101)
    with pytest.raises(ValueError):
        goal("valid", domain="")
    with pytest.raises(ValueError):
        goal("valid", deadline=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        CompletionCondition("", "x", ConditionOperator.EQ, 1)
    with pytest.raises(ValueError):
        CompletionCondition("id", "x", ConditionOperator.EQ, float("nan"))
    duplicate = CompletionCondition("same", "x", ConditionOperator.EQ, 1)
    with pytest.raises(ValueError):
        goal("valid", conditions=(duplicate, duplicate))
    with pytest.raises(ValueError):
        GoalPolicy(FakeClock(NOW), (goal("same"), goal("same")))
    with pytest.raises(ValueError):
        GoalPolicy(NaiveClock(), ())


def test_non_numeric_order_condition_is_not_satisfied() -> None:
    condition = CompletionCondition("ordered", "value", ConditionOperator.GT, 1)
    assert not condition.matches({"value": "2"})
    assert not condition.matches({"value": True})
    assert not CompletionCondition("finite", "value", ConditionOperator.GT, 1).matches(
        {"value": float("inf")}
    )
