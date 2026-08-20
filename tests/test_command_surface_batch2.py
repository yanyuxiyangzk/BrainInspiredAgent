from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

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
