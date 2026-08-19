from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from active_agent_platform import (
    CognitiveCoordinator,
    CognitiveCycle,
    CompletionCondition,
    ConditionOperator,
    CycleOutcome,
    FakeStructuredModel,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    MemoryContextSnapshot,
    PlannerError,
    PlanningRule,
    PlanSource,
    RulePlanner,
    WorldModel,
)
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.plan_validation import PlanValidationError, PlanValidator
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus

NOW = datetime(2026, 8, 18, 1, 25, tzinfo=UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{item:012d}") for item in range(1, 20))


def _cycle(clock: FakeClock) -> CognitiveCycle:
    event = EventEnvelope(
        msg_id=str(IDS[0]), msg_type="attention.salient_event", source="attention",
        occurred_at=NOW, published_at=NOW, priority=90, correlation_id=str(IDS[1]),
        dedup_key="market-summary:2026-08-18:auction",
        payload={"event_type": "attention.salient_event", "data": {"symbol": "INDEX.TEST"}},
    )
    goal = GoalDefinition(
        "market.summary", 1, 80, "market", NOW + timedelta(minutes=20),
        GoalBudget(100, 10, "CNY", 60),
        (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
    )
    goals = GoalPolicy(clock, (goal,)).evaluate({"done": False})
    coordinator = CognitiveCoordinator(clock, FakeUuidGenerator((IDS[2],)), merge_window_seconds=0)
    coordinator.submit(event)
    decision = coordinator.form_cycle(
        WorldModel(clock).snapshot, goals, MemoryContextSnapshot(0, NOW, {}), force=True
    )
    assert decision.outcome is CycleOutcome.CREATED and decision.cycle is not None
    return decision.cycle


def _rule(*, use_model: bool = False) -> PlanningRule:
    return PlanningRule(
        "market.summary.v1", "market.summary", "market_summary", "1.0.0",
        {"symbol": "INDEX.TEST"}, "summarize significant market movement",
        "market_summary", ("attention.salient_event",), use_model=use_model,
    )


def _workflow() -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "market_summary", "version": "1.0.0",
        "name": "market summary", "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
        },
        "policy": {"timeout_seconds": 60, "max_parallelism": 1, "required_capabilities": ["market.read"]},
        "nodes": [{
            "node_id": "read", "type": "skill", "depends_on": [],
            "capability": "market.read", "capability_version": "1.0",
            "input": {"symbol": "$.params.symbol"}, "constraints": {"side_effect": "PURE"},
        }],
        "output_mapping": {},
    }


@pytest.mark.asyncio
async def test_rule_planner_creates_schema_valid_immutable_candidate() -> None:
    clock = FakeClock(NOW)
    planner = RulePlanner(clock, FakeUuidGenerator(IDS[3:]), (_rule(),))
    result = await planner.plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")

    assert result.source is PlanSource.RULE
    assert result.plan.document["correlation_id"] == str(IDS[1])
    tasks = cast(tuple[object, ...], result.plan.document["tasks"])
    task = cast(dict[str, object], tasks[0])
    assert task["workflow_id"] == "market_summary"
    assert task["idempotency_key"] == "market_summary:market-summary:2026-08-18:auction"
    assert result.plan.document["requested_budget"] == {
        "max_tokens": 100, "max_cost_minor": 10, "currency": "CNY", "max_duration_seconds": 60,
    }
    with pytest.raises(TypeError):
        task["workflow_id"] = "changed"

    registry = WorkflowRegistry()
    registered = registry.register(_workflow(), status=WorkflowStatus.VALIDATED)
    registry.activate(registered.workflow_id, registered.version)
    assert PlanValidator(registry).validate(result.plan.document, now=NOW).plan == result.plan


@pytest.mark.asyncio
async def test_structured_fake_model_can_propose_workflow_intent() -> None:
    clock = FakeClock(NOW)
    model = FakeStructuredModel(({
        "workflow_id": "market_summary", "workflow_version": "1.0.0",
        "params": {"symbol": "MODEL.TEST"}, "reason": "model structured reason",
    },))
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=model
    ).plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    assert result.source is PlanSource.MODEL and result.model_error is None
    assert len(model.requests) == 1
    task = cast(tuple[Mapping[str, object], ...], result.plan.document["tasks"])[0]
    assert task["params"] == {"symbol": "MODEL.TEST"}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ("not-json", RuntimeError("offline")))
async def test_model_failure_falls_back_without_model_fields(response: object) -> None:
    clock = FakeClock(NOW)
    model = FakeStructuredModel((cast(str | Exception, response),))
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=model
    ).plan(_cycle(clock), brain_mode="SAFE", phase="AUCTION")
    assert result.source is PlanSource.RULE_FALLBACK and result.model_error
    task = cast(tuple[Mapping[str, object], ...], result.plan.document["tasks"])[0]
    assert task["params"] == {"symbol": "INDEX.TEST"}


@pytest.mark.asyncio
async def test_model_semantic_output_still_requires_f04_validation() -> None:
    clock = FakeClock(NOW)
    model = FakeStructuredModel(({
        "workflow_id": "unknown", "workflow_version": "1.0.0",
        "params": {}, "reason": "looks structured but is not authorized",
    },))
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=model
    ).plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    with pytest.raises(PlanValidationError, match="registered") as caught:
        PlanValidator(WorkflowRegistry()).validate(result.plan.document, now=NOW)
    assert caught.value.code == "WORKFLOW_NOT_FOUND"


@pytest.mark.asyncio
async def test_planner_rejects_unmatched_cycle_and_invalid_configuration() -> None:
    clock = FakeClock(NOW)
    unmatched = PlanningRule(
        "other", "other.goal", "other", "1.0.0", {}, "other", "other"
    )
    with pytest.raises(PlannerError) as caught:
        await RulePlanner(clock, FakeUuidGenerator(IDS[3:]), (unmatched,)).plan(
            _cycle(clock), brain_mode="NORMAL", phase="AUCTION"
        )
    assert caught.value.code == "NO_PLANNING_RULE"
    with pytest.raises(ValueError, match="unique"):
        RulePlanner(clock, FakeUuidGenerator(IDS), (_rule(), _rule()))
    with pytest.raises(ValueError, match="TTL"):
        PlanningRule("bad", "goal", "flow", "1.0.0", {}, "x", "x", task_ttl_seconds=2, plan_ttl_seconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ({}, [], {"workflow_id": "x"}, {"workflow_id": "x", "workflow_version": "1.0.0", "params": {}, "reason": ""}))
async def test_invalid_model_shapes_use_deterministic_fallback(response: object) -> None:
    clock = FakeClock(NOW)
    model = FakeStructuredModel((cast(str | Mapping[str, object] | Exception, response),))
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=model
    ).plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    assert result.source is PlanSource.RULE_FALLBACK


@pytest.mark.asyncio
async def test_no_model_and_unavailable_goal_are_explicit() -> None:
    clock = FakeClock(NOW)
    planner = RulePlanner(clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),))
    result = await planner.plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    assert result.source is PlanSource.RULE_FALLBACK and result.model_error == "MODEL_UNAVAILABLE"
    original = _cycle(clock)
    cycle = replace(original, goal_snapshot=replace(original.goal_snapshot, evaluations={}))
    with pytest.raises(PlannerError, match="selected") as caught:
        await RulePlanner(clock, FakeUuidGenerator(IDS[3:]), (_rule(),)).plan(
            cycle, brain_mode="NORMAL", phase="AUCTION"
        )
    assert caught.value.code == "GOAL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_model_exhaustion_and_non_object_output_fall_back() -> None:
    clock = FakeClock(NOW)
    model = FakeStructuredModel(("[]",))
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=model
    ).plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    assert result.source is PlanSource.RULE_FALLBACK
    exhausted = FakeStructuredModel(())
    result = await RulePlanner(
        clock, FakeUuidGenerator(IDS[3:]), (_rule(use_model=True),), model=exhausted
    ).plan(_cycle(clock), brain_mode="NORMAL", phase="AUCTION")
    assert result.model_error == "MODEL_UNAVAILABLE"


def test_planning_rule_rejects_empty_and_invalid_durations() -> None:
    with pytest.raises(ValueError, match="identifiers"):
        PlanningRule("", "goal", "flow", "1.0.0", {}, "x", "x")
    with pytest.raises(ValueError, match="positive"):
        PlanningRule("bad", "goal", "flow", "1.0.0", {}, "x", "x", data_freshness_seconds=0)
