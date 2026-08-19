from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from active_agent_platform.workflow_runs import (
    NODE_TERMINAL,
    WORKFLOW_TERMINAL,
    NodeRunStatus,
    RunStateError,
    WorkflowRunRepository,
    WorkflowRunStatus,
)

NOW = datetime(2026, 8, 17, tzinfo=UTC)


async def seed_task(transaction: SQLiteTransaction, task_id: str = "task-1") -> None:
    await transaction.execute(
        """
        INSERT INTO plan(plan_id, plan_json, digest, status, created_at, expires_at, correlation_id)
        VALUES ('plan-1', '{}', 'digest', 'APPROVED', 'now', 'later', 'corr-1')
        """
    )
    await transaction.execute(
        """
        INSERT INTO plan_decision(
            decision_id, plan_id, decision, decision_json, decided_at, correlation_id
        ) VALUES ('decision-1', 'plan-1', 'APPROVED', '{}', 'now', 'corr-1')
        """
    )
    await transaction.execute(
        """
        INSERT INTO execution_grant(
            grant_id, decision_id, task_id, grant_json, status,
            issued_at, expires_at, correlation_id
        ) VALUES ('grant-1', 'decision-1', ?, '{}', 'ACTIVE', 'now', 'later', 'corr-1')
        """,
        (task_id,),
    )
    await transaction.execute(
        """
        INSERT INTO task(task_id, grant_id, status, created_at, deadline, correlation_id)
        VALUES (?, 'grant-1', 'PENDING', 'now', 'later', 'corr-1')
        """,
        (task_id,),
    )


async def create_workflow(repository: WorkflowRunRepository) -> object:
    return await repository.create_workflow(
        run_id="run-1",
        task_id="task-1",
        workflow_id="market_summary",
        workflow_version="1.0.0",
        workflow_digest="sha256:workflow",
        input_digest="sha256:input",
        deadline=NOW + timedelta(minutes=5),
        created_at=NOW,
        correlation_id="corr-1",
        transition_id="wt-0",
        event_id="we-0",
    )


@pytest.mark.asyncio
async def test_workflow_and_node_happy_path_is_append_only(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runs.db")
    await database.initialize()
    async with database.transaction() as transaction:
        await seed_task(transaction)
        repository = WorkflowRunRepository(transaction)
        workflow = await create_workflow(repository)
        assert workflow.status is WorkflowRunStatus.PENDING and workflow.version == 0
        workflow = await repository.transition_workflow(
            "run-1", WorkflowRunStatus.READY, expected_version=0, reason="validated",
            occurred_at=NOW + timedelta(seconds=1), transition_id="wt-1", event_id="we-1",
        )
        workflow = await repository.transition_workflow(
            "run-1", WorkflowRunStatus.RUNNING, expected_version=1, reason="dispatched",
            occurred_at=NOW + timedelta(seconds=2), transition_id="wt-2", event_id="we-2",
        )
        node = await repository.create_node(
            run_id="run-1", node_id="fetch", attempt=1, created_at=NOW,
            correlation_id="corr-1", transition_id="nt-0", event_id="ne-0",
            skill_binding_id="binding-1", input_artifact_id="input-1",
        )
        assert node.status is NodeRunStatus.PENDING and node.version == 0
        node = await repository.transition_node(
            "run-1", "fetch", 1, NodeRunStatus.READY, expected_version=0,
            reason="dependencies met", occurred_at=NOW + timedelta(seconds=1),
            transition_id="nt-1", event_id="ne-1",
        )
        node = await repository.transition_node(
            "run-1", "fetch", 1, NodeRunStatus.RUNNING, expected_version=1,
            reason="invoked", occurred_at=NOW + timedelta(seconds=2),
            transition_id="nt-2", event_id="ne-2",
        )
        node = await repository.transition_node(
            "run-1", "fetch", 1, NodeRunStatus.SUCCEEDED, expected_version=2,
            reason="completed", occurred_at=NOW + timedelta(seconds=3),
            transition_id="nt-3", event_id="ne-3", output_artifact_id="output-1",
            usage={"cost": 0.0, "tokens": 0},
        )
        workflow = await repository.transition_workflow(
            "run-1", WorkflowRunStatus.SUCCEEDED, expected_version=2, reason="all nodes succeeded",
            occurred_at=NOW + timedelta(seconds=4), transition_id="wt-3", event_id="we-3",
        )
        assert node.output_artifact_id == "output-1" and node.usage == {"cost": 0.0, "tokens": 0}
        assert node.started_at == NOW + timedelta(seconds=2)
        assert workflow.status in WORKFLOW_TERMINAL and workflow.finished_at == NOW + timedelta(seconds=4)
        assert [item.to_status for item in await repository.workflow_history("run-1")] == ["PENDING", "READY", "RUNNING", "SUCCEEDED"]
        assert [item.version for item in await repository.node_history("run-1", "fetch", 1)] == [0, 1, 2, 3]
    await database.close()


@pytest.mark.asyncio
async def test_failure_requires_error_and_supports_next_attempt(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "failure.db")
    await database.initialize()
    async with database.transaction() as transaction:
        await seed_task(transaction)
        repository = WorkflowRunRepository(transaction)
        await create_workflow(repository)
        await repository.create_node(
            run_id="run-1", node_id="summarize", attempt=1, created_at=NOW,
            correlation_id="corr-1", transition_id="n0", event_id="e0",
        )
        await repository.transition_node(
            "run-1", "summarize", 1, NodeRunStatus.READY, expected_version=0, reason="ready",
            occurred_at=NOW, transition_id="n1", event_id="e1",
        )
        await repository.transition_node(
            "run-1", "summarize", 1, NodeRunStatus.RUNNING, expected_version=1, reason="start",
            occurred_at=NOW, transition_id="n2", event_id="e2",
        )
        with pytest.raises(RunStateError, match="error_id"):
            await repository.transition_node(
                "run-1", "summarize", 1, NodeRunStatus.FAILED, expected_version=2,
                reason="failed", occurred_at=NOW, transition_id="bad", event_id="bad",
            )
        failed = await repository.transition_node(
            "run-1", "summarize", 1, NodeRunStatus.FAILED, expected_version=2,
            reason="provider error", occurred_at=NOW, transition_id="n3", event_id="e3",
            error_id="error-1",
        )
        assert failed.status in NODE_TERMINAL and failed.error_id == "error-1"
        retry = await repository.create_node(
            run_id="run-1", node_id="summarize", attempt=2, created_at=NOW,
            correlation_id="corr-1", transition_id="n4", event_id="e4",
        )
        assert retry.attempt == 2
        with pytest.raises(RunStateError, match="sequential"):
            await repository.create_node(
                run_id="run-1", node_id="summarize", attempt=4, created_at=NOW,
                correlation_id="corr-1", transition_id="n5", event_id="e5",
            )
    await database.close()


@pytest.mark.asyncio
async def test_invalid_transitions_version_and_event_conflicts_are_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid.db")
    await database.initialize()
    async with database.transaction() as transaction:
        await seed_task(transaction)
        repository = WorkflowRunRepository(transaction)
        await create_workflow(repository)
        with pytest.raises(RunStateError, match="invalid transition"):
            await repository.transition_workflow(
                "run-1", WorkflowRunStatus.SUCCEEDED, expected_version=0, reason="skip",
                occurred_at=NOW, transition_id="x", event_id="x",
            )
        await repository.transition_workflow(
            "run-1", WorkflowRunStatus.READY, expected_version=0, reason="ready",
            occurred_at=NOW, transition_id="w1", event_id="shared",
        )
        same = await repository.transition_workflow(
            "run-1", WorkflowRunStatus.READY, expected_version=0, reason="ignored",
            occurred_at=NOW, transition_id="ignored", event_id="shared",
        )
        assert same.version == 1
        with pytest.raises(RunStateError, match="version conflict"):
            await repository.transition_workflow(
                "run-1", WorkflowRunStatus.RUNNING, expected_version=0, reason="stale",
                occurred_at=NOW, transition_id="w2", event_id="new",
            )
        with pytest.raises(RunStateError, match="another transition"):
            await repository.transition_workflow(
                "run-1", WorkflowRunStatus.RUNNING, expected_version=1, reason="conflict",
                occurred_at=NOW, transition_id="w3", event_id="we-0",
            )
        with pytest.raises(RunStateError, match="not found") as error:
            await repository.get_node("run-1", "missing", 1)
        assert error.value.code == "NODE_RUN_NOT_FOUND"
    await database.close()


@pytest.mark.asyncio
async def test_creation_validation_and_transaction_rollback(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "rollback.db")
    await database.initialize()
    with pytest.raises(RunStateError, match="deadline"):
        async with database.transaction() as transaction:
            await seed_task(transaction)
            await WorkflowRunRepository(transaction).create_workflow(
                run_id="run-1", task_id="task-1", workflow_id="wf", workflow_version="1.0.0",
                workflow_digest="d", input_digest="i", deadline=NOW, created_at=NOW,
                correlation_id="corr", transition_id="t", event_id="e",
            )
    assert await database.fetch_one("SELECT * FROM task WHERE task_id = 'task-1'") is None
    async with database.transaction() as transaction:
        await seed_task(transaction)
        repository = WorkflowRunRepository(transaction)
        with pytest.raises(RunStateError, match="identifiers"):
            await repository.create_workflow(
                run_id="", task_id="task-1", workflow_id="wf", workflow_version="1.0.0",
                workflow_digest="d", input_digest="i", deadline=NOW + timedelta(seconds=1),
                created_at=NOW, correlation_id="corr", transition_id="t", event_id="e",
            )
        with pytest.raises(RunStateError, match="not found") as error:
            await repository.get_workflow("missing")
        assert error.value.code == "WORKFLOW_RUN_NOT_FOUND"
    await database.close()


@pytest.mark.asyncio
async def test_migration_adds_append_only_fact_tables(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "migration.db")
    await database.initialize()
    tables = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%run_transition' ORDER BY name"
    )
    assert [row["name"] for row in tables] == ["node_run_transition", "workflow_run_transition"]
    migrations = await database.fetch_all("SELECT version FROM schema_migration ORDER BY version")
    assert migrations[-1]["version"] == "023_command_execution_runtime"
    await database.close()
