from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform.foundation.identity import FakeUuidGenerator
from active_agent_platform.motor import MotorExec, MotorExecutionRequest, TaskStatus
from active_agent_platform.plan_validation import (
    PlanValidationError,
    PlanValidator,
    _assert_acyclic,
)
from active_agent_platform.planning import (
    CandidatePlan,
    GrantIssuer,
    PlanDecision,
    PlanningError,
    PlanningRepository,
)
from active_agent_platform.risk import RiskBudget, RiskGate, RiskPolicy, RiskRejected
from active_agent_platform.skill_recovery import RecoveryAction, SkillRecoveryResult
from active_agent_platform.skills import SkillBinding
from active_agent_platform.state import BrainMode, BrainState, MarketPhase, Workload
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.trace import TraceQuery, TraceRepository
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runs import WorkflowRunStatus
from active_agent_platform.workflow_runtime import WorkflowExecutionResult

NOW = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)
PLAN_ID = "00000000-0000-0000-0000-000000000001"
TASK_ID = "00000000-0000-0000-0000-000000000002"
CORR = "00000000-0000-0000-0000-000000000003"
DECISION_ID = "00000000-0000-0000-0000-000000000004"
GRANT_ID = "00000000-0000-0000-0000-000000000005"


class Clock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        del seconds


class Runtime:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def execute(self, request: object) -> WorkflowExecutionResult:
        self.requests.append(request)
        return WorkflowExecutionResult("run", WorkflowRunStatus.SUCCEEDED, {"ok": True}, {})

    async def cancel(self, run_id: str) -> bool:
        del run_id
        return True


def workflow_document() -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "summary", "version": "1.0.0",
        "name": "summary", "input_schema": {
            "type": "object", "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
        "policy": {"timeout_seconds": 60, "max_parallelism": 1, "required_capabilities": ["market.read"]},
        "nodes": [{
            "node_id": "read", "type": "skill", "depends_on": [],
            "capability": "market.read", "capability_version": "1.0",
            "input": {"symbol": "$.params.symbol"}, "constraints": {"side_effect": "PURE"},
        }],
        "output_mapping": {},
    }


def plan_document(*, symbol: object = "600000", fresh_until: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0", "plan_id": PLAN_ID, "status": "CANDIDATE",
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "correlation_id": CORR,
        "trigger": {"type": "GOAL", "source_id": "goal", "occurred_at": NOW.isoformat()},
        "goal": {"goal_id": "market.summary", "priority": 80}, "reason": "summarize", "evidence": [],
        "tasks": [{
            "task_id": TASK_ID, "workflow_id": "summary", "workflow_version": "1.0.0",
            "params": {"symbol": symbol}, "priority": 80,
            "deadline": (NOW + timedelta(minutes=5)).isoformat(), "idempotency_key": "summary:600000",
            "depends_on": [],
        }],
        "requested_budget": {"max_tokens": 100, "max_cost_minor": 10, "currency": "CNY", "max_duration_seconds": 60},
        "policy_context": {"brain_mode": "NORMAL", "market_phase": "TRADING", "data_fresh_until": (fresh_until or NOW + timedelta(minutes=1)).isoformat()},
    }


def active_workflow(registry: WorkflowRegistry):
    item = registry.register(workflow_document(), status=WorkflowStatus.VALIDATED)
    return registry.activate(item.workflow_id, item.version)


def binding() -> SkillBinding:
    return SkillBinding("read", "market.read", "1.0", "fake-market", "1.0.0", "sha256:" + "a" * 64, "policy-1", NOW)


@pytest.mark.asyncio
async def test_governed_execution_is_traceable_end_to_end(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    workflow = active_workflow(registry)
    validated = PlanValidator(registry).validate(plan_document(), now=NOW)
    policy = RiskPolicy(
        "policy-1", frozenset({"market.read"}), frozenset({"market.read"}),
        RiskBudget(1000, 100, 600), RiskBudget(500, 50, 300), frozenset({"market.read"}),
    )
    approval = RiskGate(policy).evaluate(
        validated,
        state=BrainState(MarketPhase.TRADING, Workload.IDLE, BrainMode.NORMAL, NOW),
        now=NOW,
    )
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    bind = binding()
    decision_doc = {
        "schema_version": "1.0", "decision_id": DECISION_ID, "plan_id": PLAN_ID,
        "decision": "APPROVED", "decided_at": NOW.isoformat(), "validator_version": "1",
        "policy_version": approval.policy_version, "world_snapshot_id": "world-1",
        "reasons": ["allowed"], "correlation_id": CORR,
    }
    grant_doc = {
        "schema_version": "1.0", "grant_id": GRANT_ID, "decision_id": DECISION_ID,
        "plan_id": PLAN_ID, "task_id": TASK_ID,
        "workflow": {"workflow_id": workflow.workflow_id, "version": workflow.version, "digest": workflow.digest},
        "bindings": [{
            "schema_version": "1.0", "node_id": bind.node_id, "capability": bind.capability,
            "capability_version": bind.capability_version, "skill_id": bind.skill_id,
            "skill_version": bind.skill_version, "skill_digest": bind.skill_digest,
            "binding_policy_version": bind.binding_policy_version, "resolved_at": NOW.isoformat(),
        }],
        "policy_version": "policy-1", "world_snapshot_id": "world-1", "memory_snapshot_id": "memory-1",
        "allowed_permissions": ["market.read"],
        "budget": {"max_duration_seconds": 60, "max_tokens": 100, "max_cost_minor": 10, "currency": "CNY"},
        "issued_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=6)).isoformat(),
        "consumption": "SINGLE_TASK_MULTI_ATTEMPT", "correlation_id": CORR,
    }
    async with database.transaction() as transaction:
        plans = PlanningRepository(transaction)
        await plans.add_plan(CandidatePlan.create(plan_document()))
        await plans.add_decision(PlanDecision.create(decision_doc))
        await GrantIssuer(transaction).issue(grant_doc)
    runtime = Runtime()
    motor = MotorExec(
        database, runtime, clock=Clock(),
        identifiers=FakeUuidGenerator(UUID(int=index) for index in range(100, 140)),  # type: ignore[arg-type]
    )
    base = MotorExecutionRequest(
        GRANT_ID,
        TASK_ID,
        workflow,
        {"symbol": "600000"},
        {(workflow.workflow_id, workflow.version, "read"): bind},
        NOW + timedelta(minutes=5),
        frozenset({"market.read"}),
    )
    for invalid, code in (
        (replace(base, task_id="other"), "GRANT_TASK_MISMATCH"),
        (replace(base, workflow=replace(workflow, digest="sha256:bad")), "GRANT_WORKFLOW_MISMATCH"),
        (replace(base, deadline=NOW + timedelta(minutes=7)), "GRANT_EXPIRED"),
        (replace(base, allowed_permissions=frozenset({"admin"})), "GRANT_PERMISSION_DENIED"),
        (replace(base, bindings={}), "GRANT_BINDING_MISMATCH"),
    ):
        with pytest.raises(PlanningError) as error:
            await motor.execute(invalid)
        assert error.value.code == code
    result = await motor.execute(
        base
    )
    assert result.status is WorkflowRunStatus.SUCCEEDED
    task = await database.fetch_one("SELECT status, attempt FROM task WHERE task_id = ?", (TASK_ID,))
    assert task is not None and tuple(task) == (TaskStatus.SUCCEEDED, 1)
    async with database.transaction() as transaction:
        trace = TraceRepository(transaction)
        await trace.add_episode("episode-1", TASK_ID, {"outcome": "ok"}, created_at=NOW, correlation_id=CORR)
        first = await trace.audit("issued", "grant", GRANT_ID, {"status": "ACTIVE"}, occurred_at=NOW, correlation_id=CORR)
        second = await trace.audit("completed", "task", TASK_ID, {"status": "SUCCEEDED"}, occurred_at=NOW + timedelta(seconds=1), correlation_id=CORR)
        assert first != second
    bundle = await TraceQuery(database).by_correlation(CORR)
    assert (len(bundle.plans), len(bundle.decisions), len(bundle.grants), len(bundle.tasks)) == (1, 1, 1, 1)
    assert len(bundle.workflow_runs) == 1 and len(bundle.episodes) == 1 and len(bundle.audits) == 2
    assert await motor.cancel("missing") is False
    with pytest.raises(ValueError, match="correlation"):
        await TraceQuery(database).by_correlation("")
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE task SET status = 'RUNNING', finished_at = NULL WHERE task_id = ?", (TASK_ID,)
        )
    recovered = await motor.recover(
        base,
        SkillRecoveryResult(RecoveryAction.COMPLETE, "provider confirms success"),
    )
    assert recovered.task.status is TaskStatus.SUCCEEDED and recovered.execution is None
    for action, expected in (
        (RecoveryAction.FAIL, TaskStatus.FAILED),
        (RecoveryAction.REQUIRE_REVIEW, TaskStatus.REQUIRES_REVIEW),
        (RecoveryAction.TIME_OUT, TaskStatus.TIMED_OUT),
    ):
        async with database.transaction() as transaction:
            await transaction.execute(
                "UPDATE task SET status = 'RUNNING', finished_at = NULL WHERE task_id = ?",
                (TASK_ID,),
            )
        recovered = await motor.recover(base, SkillRecoveryResult(action, "recovered"))
        assert recovered.task.status is expected
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE task SET status = 'RUNNING', finished_at = NULL WHERE task_id = ?", (TASK_ID,)
        )
    replayed = await motor.recover(
        base,
        SkillRecoveryResult(RecoveryAction.REPLAY, "safe replay", next_attempt=2),
    )
    assert replayed.task.status is TaskStatus.SUCCEEDED
    assert replayed.execution is not None
    await database.close()


def test_plan_validator_and_risk_gate_reject_before_execution() -> None:
    registry = WorkflowRegistry()
    active_workflow(registry)
    validator = PlanValidator(registry)
    with pytest.raises(PlanValidationError) as error:
        validator.validate(plan_document(symbol=3), now=NOW)
    assert error.value.code == "PARAMETER_INVALID"
    validated = validator.validate(plan_document(fresh_until=NOW - timedelta(seconds=1)), now=NOW)
    policy = RiskPolicy(
        "p", frozenset({"market.read"}), frozenset(),
        RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600), frozenset(),
    )
    with pytest.raises(RiskRejected) as error:
        RiskGate(policy).evaluate(
            validated,
            state=BrainState(MarketPhase.TRADING, Workload.IDLE, BrainMode.NORMAL, NOW), now=NOW,
        )
    assert error.value.code == "DATA_STALE"
    valid = validator.validate(plan_document(), now=NOW)
    with pytest.raises(RiskRejected) as error:
        RiskGate(policy).evaluate(
            valid, state=BrainState(MarketPhase.TRADING, Workload.IDLE, BrainMode.SAFE, NOW), now=NOW,
        )
    assert error.value.code == "BRAIN_MODE_DENIED"
    _assert_acyclic({"a": ("c",), "b": ("c",), "c": ()})


def test_plan_validator_rejects_schema_expiry_registry_and_graph() -> None:
    registry = WorkflowRegistry()
    active_workflow(registry)
    validator = PlanValidator(registry)
    missing = plan_document()
    del missing["tasks"]
    with pytest.raises(PlanValidationError) as error:
        validator.validate(missing, now=NOW)
    assert error.value.code == "PLAN_SCHEMA_INVALID"
    with pytest.raises(PlanValidationError) as error:
        validator.validate(plan_document(), now=NOW + timedelta(minutes=11))
    assert error.value.code == "PLAN_EXPIRED"
    unknown = plan_document()
    unknown["tasks"][0]["workflow_id"] = "unknown"  # type: ignore[index]
    with pytest.raises(PlanValidationError) as error:
        validator.validate(unknown, now=NOW)
    assert error.value.code == "WORKFLOW_NOT_FOUND"
    dependency = plan_document()
    dependency["tasks"][0]["depends_on"] = ["00000000-0000-0000-0000-000000000099"]  # type: ignore[index]
    with pytest.raises(PlanValidationError) as error:
        validator.validate(dependency, now=NOW)
    assert error.value.code == "PLAN_GRAPH_INVALID"
    cyclic = plan_document()
    first = cyclic["tasks"][0]  # type: ignore[index]
    assert isinstance(first, dict)
    second_id = "00000000-0000-0000-0000-000000000006"
    first["depends_on"] = [second_id]
    second = dict(first)
    second["task_id"] = second_id
    second["depends_on"] = [TASK_ID]
    cyclic["tasks"] = [first, second]
    with pytest.raises(PlanValidationError, match="cycle"):
        validator.validate(cyclic, now=NOW)
    late = plan_document()
    late["tasks"][0]["deadline"] = (NOW + timedelta(minutes=11)).isoformat()  # type: ignore[index]
    with pytest.raises(PlanValidationError, match="deadline"):
        validator.validate(late, now=NOW)
    inactive_registry = WorkflowRegistry()
    inactive_registry.register(workflow_document(), status=WorkflowStatus.VALIDATED)
    with pytest.raises(PlanValidationError) as error:
        PlanValidator(inactive_registry).validate(plan_document(), now=NOW)
    assert error.value.code == "WORKFLOW_NOT_ALLOWED"


def test_risk_gate_rejects_capability_and_budget_without_consumption() -> None:
    registry = WorkflowRegistry()
    active_workflow(registry)
    validated = PlanValidator(registry).validate(plan_document(), now=NOW)
    state = BrainState(MarketPhase.TRADING, Workload.IDLE, BrainMode.NORMAL, NOW)
    denied = RiskPolicy(
        "p", frozenset(), frozenset(), RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600)
    )
    with pytest.raises(RiskRejected) as error:
        RiskGate(denied).evaluate(validated, state=state, now=NOW)
    assert error.value.code == "CAPABILITY_DENIED"
    low_budget = RiskPolicy(
        "p", frozenset({"market.read"}), frozenset(),
        RiskBudget(10, 1, 10), RiskBudget(1000, 100, 600),
    )
    with pytest.raises(RiskRejected) as error:
        RiskGate(low_budget).evaluate(validated, state=state, now=NOW)
    assert error.value.code == "BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_motor_batch_orders_priority_then_task_id() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.ids: list[str] = []

        async def execute(self, request: MotorExecutionRequest) -> WorkflowExecutionResult:
            self.ids.append(request.task_id)
            return WorkflowExecutionResult(request.task_id, WorkflowRunStatus.SUCCEEDED, {}, {})

    registry = WorkflowRegistry()
    workflow = active_workflow(registry)
    low = MotorExecutionRequest("g", "b", workflow, {}, {}, NOW, frozenset(), priority=1)
    high_b = replace(low, task_id="z", priority=9)
    high_a = replace(low, task_id="a", priority=9)
    recorder = Recorder()
    results = await MotorExec.execute_batch(recorder, (low, high_b, high_a))  # type: ignore[arg-type]
    assert recorder.ids == ["a", "z", "b"]
    assert [result.run_id for result in results] == ["a", "z", "b"]
    with pytest.raises(ValueError, match="priority"):
        replace(low, priority=101)


@pytest.mark.asyncio
async def test_motor_rejects_permission_expansion_without_task_creation(tmp_path: Path) -> None:
    # Grant absence is rejected before any Task or Workflow execution can be created.
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    registry = WorkflowRegistry()
    workflow = active_workflow(registry)
    motor = MotorExec(
        database, Runtime(), clock=Clock(),
        identifiers=FakeUuidGenerator(UUID(int=index) for index in range(200, 220)),  # type: ignore[arg-type]
    )
    with pytest.raises(PlanningError) as error:
        await motor.execute(
            MotorExecutionRequest("missing", TASK_ID, workflow, {}, {}, NOW + timedelta(minutes=1), frozenset({"admin"}))
        )
    assert error.value.code == "GRANT_NOT_FOUND"
    row = await database.fetch_one("SELECT count(*) FROM task")
    assert row is not None and row[0] == 0
    await database.close()
