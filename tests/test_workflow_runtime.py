from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.foundation.identity import FakeUuidGenerator
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillError,
    SkillInvocation,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
    SkillResult,
)
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runs import (
    NodeRunStatus,
    WorkflowRunRepository,
    WorkflowRunStatus,
)
from active_agent_platform.workflow_runtime import (
    WorkflowExecutionError,
    WorkflowExecutionRequest,
    WorkflowRuntime,
)
from apps.quant_agent import SUMMARY_CAPABILITY, install_fake_skills

NOW = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = NOW
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class Logger:
    def info(self, message: str, **fields: object) -> None:
        pass


def skill(node_id: str, depends_on: list[str], items: object) -> dict[str, object]:
    return {
        "node_id": node_id,
        "type": "skill",
        "depends_on": depends_on,
        "capability": SUMMARY_CAPABILITY,
        "capability_version": "1.0",
        "input": {"title": node_id, "items": items},
        "constraints": {"side_effect": "PURE"},
    }


def workflow(
    workflow_id: str,
    nodes: list[dict[str, object]],
    output_mapping: dict[str, object],
) -> dict[str, object]:
    return {
        "spec_version": "1.0",
        "workflow_id": workflow_id,
        "version": "1.0.0",
        "name": workflow_id,
        "input_schema": {"type": "object"},
        "policy": {
            "timeout_seconds": 60,
            "max_parallelism": 2,
            "required_capabilities": [SUMMARY_CAPABILITY],
        },
        "nodes": nodes,
        "output_mapping": output_mapping,
    }


async def seed_task(database: SQLiteDatabase) -> None:
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO plan VALUES ('plan', '{}', 'd', 'APPROVED', 'now', 'later', 'corr')"
        )
        await transaction.execute(
            "INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', 'now', 'corr')"
        )
        await transaction.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', 'task', '{}', 'ACTIVE', 'now', 'later', 'corr')"
        )
        await transaction.execute(
            """
            INSERT INTO task(task_id, grant_id, status, created_at, deadline, correlation_id)
            VALUES ('task', 'grant', 'PENDING', 'now', 'later', 'corr')
            """
        )


async def create_run(
    database: SQLiteDatabase, definition: object, *, run_id: str = "root-run"
) -> None:
    from active_agent_platform.workflow import WorkflowDefinition

    assert isinstance(definition, WorkflowDefinition)
    async with database.transaction() as transaction:
        await WorkflowRunRepository(transaction).create_workflow(
            run_id=run_id,
            task_id="task",
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            workflow_digest=definition.digest,
            input_digest="sha256:input",
            deadline=NOW + timedelta(minutes=5),
            created_at=NOW,
            correlation_id="corr",
            transition_id=f"{run_id}-transition",
            event_id=f"{run_id}-event",
        )


def bindings_for(
    registry: WorkflowRegistry,
    capabilities: CapabilityRegistry,
    skills: SkillRegistry,
    clock: Clock,
) -> dict[tuple[str, str, str], object]:
    from active_agent_platform.skills import SkillBinding

    resolver = SkillResolver(capabilities, skills, clock=clock)
    bindings: dict[tuple[str, str, str], SkillBinding] = {}
    for definition in registry.all():
        for raw in definition.definition["nodes"]:  # type: ignore[union-attr]
            if raw["type"] != "skill":  # type: ignore[index]
                continue
            node_id = str(raw["node_id"])  # type: ignore[index]
            bindings[(definition.workflow_id, definition.version, node_id)] = resolver.resolve(
                SkillRequirement(node_id, SUMMARY_CAPABILITY, "1.0", frozenset(), SideEffect.PURE),
                policy_version="test",
            )
    return bindings


async def composition(
    tmp_path: Path, registry: WorkflowRegistry, *, invoker: object | None = None
) -> tuple[
    SQLiteDatabase, WorkflowRuntime, CapabilityRegistry, SkillRegistry, Clock
]:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    await seed_task(database)
    clock = Clock()
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock)
    artifacts = LocalArtifactStore(
        tmp_path / "objects", inline_limit_bytes=80, max_artifact_bytes=2_000_000
    )
    context = SkillContext(
        clock, Logger(), CancellationToken(), artifacts, {}, ResourceBudget(10)
    )
    runtime = WorkflowRuntime(
        database=database,
        registry=registry,
        skill_invoker=SkillInvoker(skills, bundle.adapters) if invoker is None else invoker,  # type: ignore[arg-type]
        skill_context=context,
        artifacts=artifacts,
        clock=clock,
        identifiers=FakeUuidGenerator(UUID(int=index) for index in range(1, 300)),
    )
    return database, runtime, capabilities, skills, clock


class ControlledInvoker:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        del context
        self.started.set()
        outcome = self.outcomes.pop(0)
        if outcome == "BLOCK":
            await self.release.wait()
            return SkillResult("SUCCEEDED", {"summary": "released"})
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, SkillResult)
        return outcome

    async def cancel(self, binding: object, invocation_id: str) -> str:
        del binding
        self.cancelled.append(invocation_id)
        self.release.set()
        return "CANCELLED"


@pytest.mark.asyncio
async def test_executes_all_five_node_types_and_persists_outputs(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    child = registry.register(
        workflow("child_flow", [skill("child_skill", [], "$.params.items")], {"text": "$.nodes.child_skill.output.summary"}),
        status=WorkflowStatus.VALIDATED,
    )
    root_nodes = [
        {
            "node_id": "choose",
            "type": "condition",
            "depends_on": [],
            "expression": "$.params.enabled == true",
            "then": ["wait"],
            "else": ["unused"],
        },
        {"node_id": "wait", "type": "delay", "depends_on": ["choose"], "duration_seconds": 0.25},
        skill("unused", ["choose"], ["unused"]),
        {
            "node_id": "fanout",
            "type": "parallel",
            "depends_on": ["wait"],
            "branches": [["left"], ["right"]],
            "failure_policy": "min_success",
            "min_success": 1,
        },
        skill("left", ["fanout"], ["alpha"]),
        skill("right", ["fanout"], {}),
        {
            "node_id": "child",
            "type": "sub_workflow",
            "depends_on": ["fanout"],
            "workflow_id": child.workflow_id,
            "workflow_version": child.version,
            "input": {"items": ["nested"]},
            "failure_policy": "propagate",
        },
    ]
    root = registry.register(
        workflow("root_flow", root_nodes, {"child": "$.nodes.child.output.output.text"}),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, root)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run",
            "task",
            root,
            {"enabled": True},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=5),
            "corr",
        )
    )
    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.output == {"child": "child_skill: nested"}
    assert result.node_statuses["unused"] is NodeRunStatus.SKIPPED
    assert result.node_statuses["right"] is NodeRunStatus.FAILED
    assert result.node_statuses["fanout"] is NodeRunStatus.SUCCEEDED
    assert clock.sleeps == [0.25]
    child_row = await database.fetch_one(
        "SELECT parent_run_id, status FROM workflow_run WHERE workflow_id = 'child_flow'"
    )
    assert child_row is not None and tuple(child_row) == ("root-run", "SUCCEEDED")
    artifact_count = await database.fetch_one("SELECT count(*) FROM artifact")
    assert artifact_count is not None and artifact_count[0] >= 1
    await database.close()


@pytest.mark.asyncio
async def test_fail_fast_skips_later_branch_and_fails_workflow(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    nodes = [
        {
            "node_id": "fanout",
            "type": "parallel",
            "depends_on": [],
            "branches": [["bad"], ["never"]],
            "failure_policy": "fail_fast",
        },
        skill("bad", ["fanout"], {}),
        skill("never", ["fanout"], ["not called"]),
    ]
    definition = registry.register(
        workflow("failed_flow", nodes, {}), status=WorkflowStatus.VALIDATED
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=5), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.FAILED and result.error_id is not None
    assert result.node_statuses["bad"] is NodeRunStatus.FAILED
    assert result.node_statuses["never"] is NodeRunStatus.SKIPPED
    await database.close()


@pytest.mark.asyncio
async def test_missing_reference_becomes_node_failure_and_depth_is_guarded(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    definition = registry.register(
        workflow("missing_ref", [skill("broken", [], "$.params.missing")], {}),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=5), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.FAILED
    with pytest.raises(WorkflowExecutionError, match="depth"):
        await runtime.execute(
            WorkflowExecutionRequest(
                "x", "task", definition, {}, {}, NOW + timedelta(minutes=1), "corr", depth=8
            )
        )
    await database.close()


@pytest.mark.asyncio
async def test_false_condition_absolute_delay_and_failed_dependency(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    nodes = [
        {
            "node_id": "choose",
            "type": "condition",
            "depends_on": [],
            "expression": "$.params.enabled == true",
            "then": ["not_selected"],
            "else": ["until"],
        },
        skill("not_selected", ["choose"], ["x"]),
        {
            "node_id": "until",
            "type": "delay",
            "depends_on": ["choose"],
            "until": "2026-08-18T01:00:01+00:00",
        },
        skill("bad", ["until"], {}),
        skill("blocked", ["bad"], ["never"]),
    ]
    definition = registry.register(
        workflow("branch_flow", nodes, {}), status=WorkflowStatus.VALIDATED
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run",
            "task",
            definition,
            {"enabled": False},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=5),
            "corr",
        )
    )
    assert result.status is WorkflowRunStatus.FAILED
    assert result.node_statuses["not_selected"] is NodeRunStatus.SKIPPED
    assert result.node_statuses["until"] is NodeRunStatus.SUCCEEDED
    assert result.node_statuses["blocked"] is NodeRunStatus.SKIPPED
    assert clock.sleeps == [1.0]
    await database.close()


@pytest.mark.asyncio
async def test_sub_workflow_continue_tolerates_child_failure(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    child = registry.register(
        workflow("bad_child", [skill("bad", [], {})], {}),
        status=WorkflowStatus.VALIDATED,
    )
    parent = registry.register(
        workflow(
            "continue_parent",
            [
                {
                    "node_id": "child",
                    "type": "sub_workflow",
                    "depends_on": [],
                    "workflow_id": child.workflow_id,
                    "workflow_version": child.version,
                    "input": {},
                    "failure_policy": "continue",
                }
            ],
            {"status": "$.nodes.child.output.status"},
        ),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, parent)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run",
            "task",
            parent,
            {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=5),
            "corr",
        )
    )
    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.output == {"status": "FAILED"}
    await database.close()


@pytest.mark.asyncio
async def test_c07_workflow_deadline_cancels_remaining_nodes(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    definition = registry.register(
        workflow(
            "deadline_flow",
            [
                {"node_id": "wait", "type": "delay", "depends_on": [], "duration_seconds": 2},
                skill("never", ["wait"], ["not reached"]),
            ],
            {},
        ),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(seconds=1), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.TIMED_OUT
    assert result.node_statuses == {
        "wait": NodeRunStatus.TIMED_OUT,
        "never": NodeRunStatus.CANCELLED,
    }
    await database.close()


@pytest.mark.asyncio
async def test_c07_finite_retry_persists_each_attempt(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    failing = skill("retrying", [], {})
    failing["retry"] = {
        "max_attempts": 3,
        "backoff_seconds": 0,
        "retry_on": ["FAILED"],
    }
    definition = registry.register(
        workflow("retry_flow", [failing], {}), status=WorkflowStatus.VALIDATED
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=1), "corr",
        )
    )
    attempts = await database.fetch_all(
        "SELECT attempt, status FROM node_run WHERE node_id = 'retrying' ORDER BY attempt"
    )
    assert result.status is WorkflowRunStatus.FAILED
    assert [tuple(row) for row in attempts] == [(1, "FAILED"), (2, "FAILED"), (3, "FAILED")]
    await database.close()


@pytest.mark.asyncio
async def test_c07_explicit_compensation_and_pre_cancel(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    child = registry.register(
        workflow("compensated_child", [skill("bad", [], {})], {}),
        status=WorkflowStatus.VALIDATED,
    )
    parent = registry.register(
        workflow(
            "compensating_parent",
            [
                {
                    "node_id": "child",
                    "type": "sub_workflow",
                    "depends_on": [],
                    "workflow_id": child.workflow_id,
                    "workflow_version": child.version,
                    "input": {},
                    "failure_policy": "compensate",
                    "compensation_node": "undo",
                },
                skill("undo", ["child"], ["undo side effect"]),
            ],
            {},
        ),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, capabilities, skills, clock = await composition(tmp_path, registry)
    await create_run(database, parent)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", parent, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=1), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.FAILED
    assert result.node_statuses["undo"] is NodeRunStatus.SUCCEEDED

    cancelled = registry.register(
        workflow("cancelled_flow", [skill("never", [], ["x"])], {}),
        status=WorkflowStatus.VALIDATED,
    )
    await create_run(database, cancelled, run_id="cancelled-run")
    token = CancellationToken()
    token.cancel()
    cancelled_result = await runtime.execute(
        WorkflowExecutionRequest(
            "cancelled-run", "task", cancelled, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=1), "corr", cancellation=token,
        )
    )
    assert cancelled_result.status is WorkflowRunStatus.CANCELLED
    assert cancelled_result.node_statuses["never"] is NodeRunStatus.CANCELLED
    await database.close()


@pytest.mark.asyncio
async def test_c07_retry_succeeds_after_temporary_error_and_backoff(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    retried = skill("retried", [], ["ok"])
    retried["retry"] = {
        "max_attempts": 2,
        "backoff_seconds": 0.5,
        "retry_on": ["TEMPORARY_UNAVAILABLE"],
    }
    definition = registry.register(
        workflow("successful_retry", [retried], {}), status=WorkflowStatus.VALIDATED
    )
    invoker = ControlledInvoker(
        [
            SkillError("TEMPORARY_UNAVAILABLE", "retry me"),
            SkillResult("SUCCEEDED", {"summary": "ok"}),
        ]
    )
    database, runtime, capabilities, skills, clock = await composition(
        tmp_path, registry, invoker=invoker
    )
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=1), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert clock.sleeps == [0.5]
    await database.close()


@pytest.mark.asyncio
async def test_c07_running_cancel_reaches_active_skill(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    definition = registry.register(
        workflow("active_cancel", [skill("blocking", [], ["x"])], {}),
        status=WorkflowStatus.VALIDATED,
    )
    invoker = ControlledInvoker(["BLOCK"])
    database, runtime, capabilities, skills, clock = await composition(
        tmp_path, registry, invoker=invoker
    )
    await create_run(database, definition)
    execution = asyncio.create_task(
        runtime.execute(
            WorkflowExecutionRequest(
                "root-run", "task", definition, {},
                bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
                NOW + timedelta(minutes=1), "corr",
            )
        )
    )
    await invoker.started.wait()
    assert await runtime.cancel("missing-run") is False
    assert await runtime.cancel("root-run") is True
    result = await execution
    assert result.status is WorkflowRunStatus.CANCELLED
    assert result.node_statuses["blocking"] is NodeRunStatus.CANCELLED
    assert len(invoker.cancelled) == 1
    await database.close()


@pytest.mark.asyncio
async def test_c07_node_timeout_cancels_adapter_but_not_workflow_deadline(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    timed = skill("timed", [], ["x"])
    timed["timeout_seconds"] = 1
    definition = registry.register(
        workflow("node_timeout", [timed], {}), status=WorkflowStatus.VALIDATED
    )
    invoker = ControlledInvoker(["BLOCK"])
    database, runtime, capabilities, skills, clock = await composition(
        tmp_path, registry, invoker=invoker
    )
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {},
            bindings_for(registry, capabilities, skills, clock),  # type: ignore[arg-type]
            NOW + timedelta(minutes=1), "corr",
        )
    )
    assert result.status is WorkflowRunStatus.FAILED
    assert result.node_statuses["timed"] is NodeRunStatus.TIMED_OUT
    assert len(invoker.cancelled) == 1
    await database.close()


@pytest.mark.asyncio
async def test_c07_rejects_invalid_limits_deadline_and_missing_binding(tmp_path: Path) -> None:
    registry = WorkflowRegistry()
    definition = registry.register(
        workflow("invalid_runtime", [skill("node", [], ["x"])], {}),
        status=WorkflowStatus.VALIDATED,
    )
    database, runtime, _, _, _ = await composition(tmp_path, registry)
    with pytest.raises(ValueError, match="parallelism"):
        WorkflowRuntime(
            database=database,
            registry=registry,
            skill_invoker=runtime._skill_invoker,
            skill_context=runtime._skill_context,
            artifacts=runtime._artifacts,
            clock=runtime._clock,
            identifiers=runtime._identifiers,
            global_parallelism=0,
        )
    with pytest.raises(WorkflowExecutionError, match="timezone-aware"):
        await runtime.execute(
            WorkflowExecutionRequest(
                "none", "task", definition, {}, {}, NOW.replace(tzinfo=None), "corr"
            )
        )
    await create_run(database, definition)
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", definition, {}, {}, NOW + timedelta(minutes=1), "corr"
        )
    )
    assert result.status is WorkflowRunStatus.FAILED
    await database.close()
