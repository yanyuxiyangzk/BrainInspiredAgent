from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.cli import run
from apps.quant_agent.insights import MarketInsightQuery
from apps.quant_agent.query_surface import CommandSurfaceQuery
from apps.quant_agent.runtime import build_quant_runtime


async def invoke(path: Path, *args: str) -> dict[str, object]:
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", str(path), *args), stdout, stderr)
    assert code == 0, stderr.getvalue()
    return json.loads(stdout.getvalue())


@pytest.mark.asyncio
async def test_batch2_query_surface_and_catalog_are_real(tmp_path: Path) -> None:
    path = tmp_path / "surface.db"
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    await components.service.stop()
    await components.database.close()

    assert (await invoke(path, "brain", "state"))["derived"] is True
    assert "areas" in await invoke(path, "brain", "areas")
    assert "cycles" in await invoke(path, "brain", "cycles")
    assert "events" in await invoke(path, "events", "recent")
    assert "events" in await invoke(path, "events", "show", "missing")
    assert "events" in await invoke(path, "events", "correlation", "missing")
    assert "events" in await invoke(path, "events", "outbox")
    assert "events" in await invoke(path, "events", "inbox")
    assert "events" in await invoke(path, "events", "dead-letter")
    assert "plans" in await invoke(path, "plans", "recent")
    assert "plans" in await invoke(path, "plans", "show", "missing")
    assert "plans" in await invoke(path, "plans", "rejected")
    assert "tasks" in await invoke(path, "tasks", "list")
    assert "tasks" in await invoke(path, "tasks", "failed")
    assert "transitions" in await invoke(path, "tasks", "show", "missing")
    assert "transitions" in await invoke(path, "tasks", "trace", "missing")
    assert len((await invoke(path, "catalog", "capabilities"))["capabilities"]) == 3
    assert len((await invoke(path, "skills", "list"))["skills"]) == 3
    assert len((await invoke(path, "workflows", "active"))["workflows"]) == 2
    assert len((await invoke(path, "skills", "show", "fake-summary"))["skills"]) == 1
    for view in ("list", "active", "show", "lineage", "explain", "executions"):
        assert "dna" in (await invoke(path, "dna", view, "missing")) or view == "executions"
    active_dna = (await invoke(path, "dna", "active", "--limit", "10"))["dna"]
    assert len(active_dna) == 4
    assert {item["kind"] for item in active_dna} == {"organization", "agent", "workflow"}


@pytest.mark.asyncio
async def test_batch2_system_and_subscription_preferences(tmp_path: Path) -> None:
    path = tmp_path / "system.db"
    await invoke(path, "start")
    for view in ("status", "health", "diagnose", "metrics", "logs", "migrations"):
        assert await invoke(path, "system", view)
    added = await invoke(
        path, "subscriptions", "add", "me", "--quiet-start-hour", "22",
        "--quiet-end-hour", "8", "--hourly-limit", "5",
    )
    assert added["status"] == "SUBSCRIBED"
    listed = await invoke(path, "subscriptions", "list")
    assert listed["subscriptions"][0]["quiet_start_hour"] == 22  # type: ignore[index]
    assert (await invoke(path, "subscriptions", "list", "me"))["deliveries"] == []
    assert (await invoke(path, "subscriptions", "disable", "me"))["changed"] is True
    assert (await invoke(path, "subscriptions", "enable", "me"))["changed"] is True
    assert (await invoke(path, "subscriptions", "disable", "missing"))["changed"] is False
    governed = await invoke(path, "tasks", "cancel", "missing")
    assert governed["governed"] is True and governed["command"] == "task.cancel"


@pytest.mark.asyncio
async def test_batch2_rejects_missing_task_control_id(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bad.db"), "tasks", "retry"), stdout, stderr,
    )
    assert code == 2 and "task identifier is required" in stderr.getvalue()
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bad.db"), "events", "recent", "--limit", "0"),
        stdout, stderr,
    )
    assert code == 2 and "limit must be between" in stderr.getvalue()


@pytest.mark.asyncio
async def test_batch2_query_validation_and_subscription_lookup(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "validation.db")
    await database.initialize()
    with pytest.raises(ValueError, match="limit"):
        await MarketInsightQuery(database).latest(limit=0)
    with pytest.raises(ValueError, match="stale"):
        await MarketInsightQuery(database).latest(stale="bad")
    subscriptions = await CommandSurfaceQuery(database).subscriptions("missing", 1)
    assert subscriptions == {"subscriptions": []}
    await database.close()

    components = build_quant_runtime(tmp_path / "control.db")
    await components.database.initialize()
    with pytest.raises(ValueError, match="does not exist"):
        await components.service._execute_task_control("task.cancel", {"task_id": "missing"}, "c")
    await components.database.close()


@pytest.mark.asyncio
async def test_u05_retry_reuses_fixed_grant_and_binding(tmp_path: Path) -> None:
    path = tmp_path / "retry.db"
    await invoke(path, "market", "summary", "--symbols", "INDEX.RETRY")
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    assert (await components.service._relay.publish_due()).published == 1
    assert await components.service.process_one()
    task = await components.database.fetch_one("SELECT task_id FROM task")
    assert task is not None
    task_id = str(task["task_id"])
    async with components.database.transaction() as transaction:
        await transaction.execute(
            "UPDATE task SET status='FAILED',finished_at=? WHERE task_id=?",
            (components.service._clock.now().isoformat(), task_id),
        )
    result = await components.service._execute_task_control(
        "task.retry", {"task_id": task_id}, "retry-correlation",
    )
    assert result["status"] == "SUCCEEDED" and result["attempt"] == 2
    attempts = await components.database.fetch_one(
        "SELECT count(*) AS total FROM grant_attempt WHERE task_id=?", (task_id,),
    )
    assert attempts is not None and attempts["total"] == 2
    await components.service.stop()
    await components.database.close()


@pytest.mark.asyncio
async def test_u05_cancel_requires_and_uses_live_motor_handle(tmp_path: Path) -> None:
    path = tmp_path / "cancel.db"
    await invoke(path, "market", "summary", "--symbols", "INDEX.CANCEL")
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    await components.service._relay.publish_due()
    await components.service.process_one()
    task = await components.database.fetch_one("SELECT task_id FROM task")
    assert task is not None
    task_id = str(task["task_id"])
    async with components.database.transaction() as transaction:
        await transaction.execute("UPDATE task SET status='RUNNING' WHERE task_id=?", (task_id,))
    cancel = AsyncMock(return_value=True)
    components.service._facade.cancel = cancel  # type: ignore[method-assign]
    result = await components.service._execute_task_control(
        "task.cancel", {"task_id": task_id}, "cancel-correlation",
    )
    assert result["status"] == "CANCEL_REQUESTED"
    cancel.assert_awaited_once_with(task_id)
    await components.service.stop()
    await components.database.close()


@pytest.mark.asyncio
async def test_u10_schedule_query_and_governed_trigger_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "schedule.db"
    listing = await invoke(path, "schedules", "list")
    assert listing["schedules"][0]["timezone"] == "Asia/Shanghai"  # type: ignore[index]
    first = await invoke(path, "schedules", "trigger", "quant.daily_review")
    second = await invoke(path, "schedules", "trigger", "quant.daily_review")
    assert first["governed"] is True and second["status"] == "PUBLISHED"
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    assert (await components.service._relay.publish_due()).published >= 1
    assert await components.service.process_one()
    assert not await components.service.process_one()
    commands = await components.database.fetch_one(
        "SELECT count(*) AS total FROM command_execution WHERE command='schedule.trigger'",
    )
    assert commands is not None and commands["total"] == 1
    await components.service.stop()
    await components.database.close()


@pytest.mark.asyncio
async def test_u06_u07_queries_explain_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "cognition-memory.db"
    await invoke(path, "market", "summary", "--symbols", "INDEX.MEMORY")
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    await components.service._relay.publish_due()
    await components.service.process_one()
    await components.service.stop()
    await components.database.close()
    goals = await invoke(path, "goals", "history")
    assert goals["dynamic_mutation_supported"] is False and goals["goals"]
    working = await invoke(path, "memory", "working")
    assert working["authoritative"] is False
    episodes = await invoke(path, "memory", "episodes")
    assert episodes["authoritative"] is True and episodes["memory"]
    candidates = await invoke(path, "memory", "candidates")
    assert candidates["candidate_promotion_implicit"] is False
    metrics = await invoke(path, "attention", "metrics")
    assert metrics["derived_from"] == "evidence_ledger"


@pytest.mark.asyncio
async def test_u09_catalog_governance_uses_cas_and_audit(tmp_path: Path) -> None:
    path = tmp_path / "catalog-governance.db"
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    await components.service.stop()
    await components.database.close()
    disabled = await invoke(
        path, "skills", "disable", "fake-summary", "--version", "1.0.0",
        "--revision", "0", "--reason", "maintenance", "--yes",
    )
    assert disabled["status"] == "DISABLED" and disabled["revision"] == 1
    enabled = await invoke(
        path, "skills", "enable", "fake-summary", "--version", "1.0.0",
        "--revision", "1", "--reason", "verified", "--yes",
    )
    assert enabled["status"] == "ENABLED" and enabled["revision"] == 2
    validated = await invoke(
        path, "workflows", "validate", "market_summary", "--version", "1.0.0",
        "--revision", "0", "--reason", "contract check", "--yes",
    )
    assert validated["valid"] is True
    database = SQLiteDatabase(path)
    await database.initialize()
    counts = await database.fetch_one(
        "SELECT (SELECT count(*) FROM catalog_transition),"
        "(SELECT count(*) FROM audit_record WHERE subject_type='skill')",
    )
    assert counts is not None and tuple(counts) == (2, 2)
    await database.close()


@pytest.mark.asyncio
async def test_p1_query_and_governance_negative_contracts(tmp_path: Path) -> None:
    path = tmp_path / "p1-negative.db"
    for command in (
        ("attention", "recent"), ("attention", "explain", "missing"),
        ("goals", "active"), ("goals", "show", "missing"),
        ("memory", "search", "needle"), ("schedules", "history"),
        ("schedules", "show", "missing"), ("evolution", "compare", "only-one"),
        ("evolution", "compare", "one,two"), ("evolution", "replay", "missing"),
        ("evolution", "explain", "missing"),
    ):
        assert await invoke(path, *command)
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(path), "memory", "consolidate", "missing",
         "--method", "manual-review", "--yes"), stdout, stderr,
    )
    assert code == 2 and "does not exist" in stderr.getvalue()
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(path), "skills", "disable", "missing", "--version", "1.0.0",
         "--revision", "0", "--reason", "test", "--yes"), stdout, stderr,
    )
    assert code == 4 and "not found" in stderr.getvalue()


@pytest.mark.asyncio
async def test_u09_workflow_cas_idempotence_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "workflow-governance.db"
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    await components.service.stop()
    await components.database.close()
    active = await invoke(
        path, "workflows", "activate", "market_summary", "--version", "1.0.0",
        "--revision", "0", "--reason", "already active", "--yes",
    )
    assert active["changed"] is False
    deprecated = await invoke(
        path, "workflows", "deprecate", "market_summary", "--version", "1.0.0",
        "--revision", "0", "--reason", "replacement", "--yes",
    )
    assert deprecated["status"] == "DEPRECATED" and deprecated["revision"] == 1
    activated = await invoke(
        path, "workflows", "activate", "market_summary", "--version", "1.0.0",
        "--revision", "1", "--reason", "restore", "--yes",
    )
    assert activated["status"] == "ACTIVE" and activated["revision"] == 2
    for arguments, expected in (
        (("skills", "disable", "fake-summary"), "requires ID"),
        (("skills", "disable", "fake-summary", "--version", "1.0.0", "--revision", "99",
          "--reason", "stale", "--yes"), "revision conflict"),
        (("schedules", "trigger", "missing"), "unknown schedule"),
    ):
        stdout, stderr = StringIO(), StringIO()
        code = await run(("--database", str(path), *arguments), stdout, stderr)
        assert code == 2 and expected in stderr.getvalue()


@pytest.mark.asyncio
async def test_command_surface_governance_validation_paths(tmp_path: Path) -> None:
    path = tmp_path / "validation-paths.db"
    for arguments in (
        ("dna", "transition", "missing"),
        ("dna", "transition", "missing", "--version", "1.0.0", "--to", "ACTIVE",
         "--revision", "0", "--reason", "test"),
        ("workflows", "validate", "missing", "--version", "1.0.0", "--revision", "0",
         "--reason", "test", "--yes"),
        ("skills", "disable", "fake-summary", "--version", "1.0.0", "--revision", "99",
         "--reason", "test", "--yes"),
    ):
        stdout, stderr = StringIO(), StringIO()
        code = await run(("--database", str(path), *arguments), stdout, stderr)
        assert code in {2, 4}


@pytest.mark.asyncio
async def test_query_surface_empty_and_identifier_branches(tmp_path: Path) -> None:
    path = tmp_path / "query-branches.db"
    for arguments in (
        ("attention", "metrics"), ("attention", "explain", "missing"),
        ("goals", "active"), ("goals", "show", "missing"),
        ("memory", "semantic"), ("memory", "search", "x"),
        ("memory", "candidates"), ("schedules", "show"),
        ("evolution", "replay", "missing"), ("evolution", "compare", "a,b"),
    ):
        assert await invoke(path, *arguments)
