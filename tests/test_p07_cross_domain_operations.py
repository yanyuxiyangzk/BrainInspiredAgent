from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.operations import PlatformOperations
from active_agent_platform.storage import DEFAULT_MIGRATIONS, SQLiteDatabase
from apps.brainagent_cli import run

NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)
CORRELATION = "00000000-0000-0000-0000-000000000777"


async def _research_facts(path: Path) -> None:
    database = SQLiteDatabase(path)
    await database.initialize()
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO plan VALUES (?,?,?,?,?,?,?)",
            ("research-plan", json.dumps({"domain": "research", "note": "evidence"}),
             "digest", "CANDIDATE", NOW.isoformat(), NOW.isoformat(), CORRELATION),
        )
        await transaction.executemany(
            """INSERT INTO outbox_event(
                   event_id,msg_type,envelope_json,publish_state,created_at,correlation_id
               ) VALUES (?,?,?,'PENDING',?,?)""",
            [(f"research-event-{index}", "research.note", "{}", NOW.isoformat(), CORRELATION)
             for index in range(100)],
        )
    await database.close()


@pytest.mark.asyncio
async def test_platform_operations_diagnose_research_backlog_and_trace(tmp_path: Path) -> None:
    path = tmp_path / "research-ops.db"
    await _research_facts(path)
    database = SQLiteDatabase(path)
    await database.initialize()
    operations = PlatformOperations(database, FakeClock(NOW))
    snapshot = await operations.snapshot()
    assert snapshot.health.ready is False
    assert snapshot.metrics.queues["outbox_pending"] == 100
    assert len(snapshot.migrations) == len(DEFAULT_MIGRATIONS)
    diagnostic = await operations.diagnose()
    assert "outbox backlog is 100" in diagnostic.health.reasons
    trace = await operations.trace(CORRELATION)
    assert trace.plans[0]["domain"] == "research"
    await database.close()


@pytest.mark.asyncio
async def test_generic_cli_exposes_metrics_trace_and_migrations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "research-cli.db"
    await _research_facts(path)
    prefix = ["--database", str(path)]

    assert await run([*prefix, "metrics"]) == 0
    assert json.loads(capsys.readouterr().out)["queues"]["outbox_pending"] == 100
    assert await run([*prefix, "metrics", "--prometheus"]) == 0
    assert "bia_queue_total" in capsys.readouterr().out
    assert await run([*prefix, "trace", CORRELATION]) == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["plans"][0]["domain"] == "research"
    assert await run([*prefix, "migrations"]) == 0
    assert len(json.loads(capsys.readouterr().out)["migrations"]) == len(DEFAULT_MIGRATIONS)
