from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.delivery import InsightDeliveryService

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_subscription_delivery_dedup_rate_limit_and_read_state(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "delivery.db")
    await database.initialize()
    service = InsightDeliveryService(database, FakeClock(NOW))
    await service.subscribe("user", minimum_level="WARNING", hourly_limit=1)
    assert (await service.deliver("missing", "i")).status == "NOT_SUBSCRIBED"
    assert (await service.deliver("user", "filtered", level="INFO")).status == "FILTERED"
    delivered = await service.deliver("user", "first", level="WARNING")
    assert delivered.status == "DELIVERED" and delivered.delivery_id
    assert (await service.deliver("user", "first", level="WARNING")).status == "DUPLICATE"
    assert (await service.deliver("user", "second", level="WARNING")).status == "RATE_LIMITED"
    assert await service.mark_read(delivered.delivery_id) is True
    assert await service.mark_read("missing") is False
    row = await database.fetch_one("SELECT read_at FROM insight_delivery WHERE delivery_id = ?", (delivered.delivery_id,))
    assert row is not None and row["read_at"] is not None
    assert len(await service.deliveries("user")) == 1

    await service.subscribe("quiet", quiet_start_hour=7, quiet_end_hour=9)
    assert (await service.deliver("quiet", "i")).status == "QUIET"
    with pytest.raises(ValueError):
        await service.subscribe("bad", quiet_start_hour=1)
    with pytest.raises(ValueError):
        await service.deliver("user", "bad", level="DEBUG")
    await database.close()


@pytest.mark.asyncio
async def test_restart_deduplicates_same_subscription_insight_and_channel(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    await InsightDeliveryService(database, FakeClock(NOW)).subscribe("user", hourly_limit=10)
    first = await InsightDeliveryService(database, FakeClock(NOW)).deliver("user", "insight")
    await database.close()
    restarted = SQLiteDatabase(path)
    await restarted.initialize()
    duplicate = await InsightDeliveryService(restarted, FakeClock(NOW)).deliver("user", "insight")
    assert duplicate.status == "DUPLICATE" and duplicate.delivery_id == first.delivery_id
    await restarted.close()
