from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.metrics import PlatformMetrics, prometheus
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.cli import EXIT_OK, run

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


async def _facts(database: SQLiteDatabase) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('p','{}','d','CANDIDATE',?,?, 'c')", (stamp, stamp))
        await tx.execute("INSERT INTO plan_decision VALUES ('d','p','APPROVED','{}',?,'c')", (stamp,))
        await tx.execute("INSERT INTO execution_grant VALUES ('g','d','t','{}','ACTIVE',?,?,'c')", (stamp, stamp))
        await tx.execute("INSERT INTO task(task_id,grant_id,status,created_at,deadline,correlation_id) VALUES ('t','g','RUNNING',?,?,'c')", (stamp, stamp))
        await tx.execute("INSERT INTO workflow_run(run_id,task_id,workflow_id,workflow_version,workflow_digest,input_digest,status,deadline,created_at,correlation_id) VALUES ('r','t','flow','1.0.0','d','i','RUNNING',?,?, 'c')", (stamp, stamp))
        await tx.execute("INSERT INTO node_run(run_id,node_id,attempt,status,correlation_id) VALUES ('r','n',1,'FAILED','c')")
        await tx.execute("INSERT INTO outbox_event(event_id,msg_type,envelope_json,publish_state,created_at,correlation_id) VALUES ('e','x.y','{}','PENDING',?,'c')", (stamp,))
        await tx.execute("INSERT INTO dead_letter VALUES ('dead','consumer','message','{}','error',?,'c')", (stamp,))
        await tx.execute("INSERT INTO local_notification_delivery VALUES ('notice','key','digest','title','message','INFO','{}',?,'t','r')", (stamp,))


@pytest.mark.asyncio
async def test_metrics_snapshot_covers_lag_queues_execution_cost_and_side_effects(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "metrics.db")
    await database.initialize()
    await _facts(database)
    clock = FakeClock(NOW)
    metrics = PlatformMetrics(database, clock)
    metrics.observe_loop(NOW - timedelta(seconds=1.25))
    metrics.record_model(tokens=120, cost_minor=7, cache_hit=True)
    metrics.record_model(tokens=30, cost_minor=2)
    metrics.record_duplicate_side_effect()
    snapshot = await metrics.snapshot()
    assert snapshot.loop_lag_seconds == 1.25
    assert snapshot.queues == {"inbox_pending": 0, "outbox_pending": 1, "dead_letter": 1}
    assert snapshot.tasks == {"RUNNING": 1} and snapshot.skills == {"FAILED": 1}
    assert (snapshot.model_requests, snapshot.model_tokens, snapshot.model_cost_minor) == (2, 150, 9)
    assert snapshot.model_cache_hits == 1
    assert (snapshot.side_effect_deliveries, snapshot.duplicate_side_effects) == (1, 1)
    text = prometheus(snapshot)
    assert 'bia_task_total{state="RUNNING"} 1' in text
    assert "bia_duplicate_side_effects_total 1" in text
    assert snapshot.to_dict()["model"] == {
        "requests": 2, "tokens": 150, "cost_minor": 9, "cache_hits": 1,
    }
    with pytest.raises(TypeError):
        snapshot.tasks["FAILED"] = 2  # type: ignore[index]
    with pytest.raises(ValueError):
        metrics.observe_loop(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        metrics.record_model(tokens=-1, cost_minor=0)
    await database.close()


@pytest.mark.asyncio
async def test_metrics_cli_supports_json_and_prometheus(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", str(path), "metrics"), stdout, stderr) == EXIT_OK
    assert '"loop_lag_seconds":0.0' in stdout.getvalue()
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", str(path), "metrics", "--prometheus"), stdout, stderr) == EXIT_OK
    assert stdout.getvalue().startswith("bia_loop_lag_seconds 0\n")
