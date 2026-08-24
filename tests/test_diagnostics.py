from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.diagnostics import HealthService, ProbeStatus
from active_agent_platform.foundation import FakeClock
from active_agent_platform.state import BrainMode, BrainState, MarketPhase, Workload
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


async def _degraded_facts(database: SQLiteDatabase) -> None:
    old = (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('p','{}','d','CANDIDATE',?,?, 'c')", (old, old))
        await tx.execute("INSERT INTO plan_decision VALUES ('d','p','APPROVED','{}',?,'c')", (old,))
        await tx.execute("INSERT INTO execution_grant VALUES ('g','d','t','{}','ACTIVE',?,?,'c')", (old, old))
        await tx.execute("INSERT INTO task(task_id,grant_id,status,created_at,deadline,error_id,correlation_id) VALUES ('t','g','RUNNING',?,?,'stuck','c')", (old, old))
        await tx.execute("INSERT INTO outbox_event(event_id,msg_type,envelope_json,publish_state,created_at,correlation_id) VALUES ('e','x.y','{}','PENDING',?,'c')", (old,))


@pytest.mark.asyncio
async def test_health_layers_and_diagnostic_snapshot(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "health.db")
    await database.initialize()
    await _degraded_facts(database)
    state = BrainState(MarketPhase.CLOSED, Workload.IDLE, BrainMode.SAFE, NOW)
    service = HealthService(
        database, FakeClock(NOW), brain_state=lambda: state, queue_warning_threshold=1
    )
    health = await service.check()
    assert health.liveness is ProbeStatus.HEALTHY
    assert health.readiness is ProbeStatus.DEGRADED and not health.ready
    assert health.dependencies == {"sqlite": ProbeStatus.HEALTHY, "outbox": ProbeStatus.DEGRADED}
    assert health.brain is ProbeStatus.DEGRADED
    assert health.brain_state is not None and health.brain_state["brain_mode"] == "SAFE"
    assert "outbox backlog" in " ".join(health.reasons)
    diagnostic = await service.diagnose(recent_limit=5)
    assert len(diagnostic.migrations) == 24
    assert diagnostic.metrics is not None and diagnostic.metrics.queues["outbox_pending"] == 1
    assert diagnostic.overdue_tasks[0]["task_id"] == "t"
    assert diagnostic.recent_errors[0]["error_id"] == "stuck"
    assert diagnostic.to_dict()["health"]["ready"] is False  # type: ignore[index]
    with pytest.raises(ValueError):
        await service.diagnose(recent_limit=0)
    with pytest.raises(ValueError):
        HealthService(database, FakeClock(NOW), queue_warning_threshold=0)
    await database.close()


@pytest.mark.asyncio
async def test_uninitialized_dependency_reports_unhealthy_without_crashing(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "offline.db")
    service = HealthService(database, FakeClock(NOW))
    health = await service.check()
    assert health.liveness is ProbeStatus.HEALTHY
    assert health.readiness is ProbeStatus.UNHEALTHY
    assert health.dependencies["sqlite"] is ProbeStatus.UNHEALTHY
    assert health.dependencies["outbox"] is ProbeStatus.UNKNOWN
    assert health.brain is ProbeStatus.UNKNOWN
    diagnostic = await service.diagnose()
    assert diagnostic.metrics is None and diagnostic.migrations == ()
