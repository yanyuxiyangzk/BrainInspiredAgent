from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from active_agent_platform import (
    MissedTriggerPolicy,
    Scheduler,
    ScheduleSpec,
)
from active_agent_platform.foundation import Uuid7Generator
from active_agent_platform.state import TradingCalendar
from active_agent_platform.storage import SQLiteDatabase


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Calendar(TradingCalendar):
    def __init__(self, holidays: set[datetime.date]) -> None:
        self.holidays = holidays

    def is_trading_day(self, value: datetime.date) -> bool:
        return value not in self.holidays and super().is_trading_day(value)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


async def setup(tmp_path: Path, clock: FakeClock, spec: ScheduleSpec) -> tuple[SQLiteDatabase, Scheduler]:
    database = SQLiteDatabase(tmp_path / "schedule.db")
    await database.initialize()
    scheduler = Scheduler(
        database,
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        [spec],
        poll_interval_seconds=1,
    )
    return database, scheduler


@pytest.mark.asyncio
async def test_window_fires_once_and_duplicate_ticks_are_idempotent(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 25))
    database, scheduler = await setup(
        tmp_path, clock, ScheduleSpec("auction", time(9, 25), window_seconds=30)
    )

    first = await scheduler.tick()
    clock.advance(10)
    second = await scheduler.tick()
    outbox = await database.fetch_all("SELECT event_id, msg_type, attempt FROM outbox_event")
    checkpoint = await database.fetch_all("SELECT status FROM schedule_checkpoint")

    assert (first.evaluated, first.triggered) == (1, 1)
    assert second.triggered == 0
    assert len(outbox) == 1 and outbox[0]["msg_type"] == "schedule.triggered"
    assert checkpoint[0]["status"] == "FIRED"
    await database.close()


@pytest.mark.asyncio
async def test_cooldown_blocks_next_occurrence_until_elapsed(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 25))
    database, scheduler = await setup(
        tmp_path,
        clock,
        ScheduleSpec("daily", time(9, 25), window_seconds=120, cooldown_seconds=60),
    )
    async with database.transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO schedule_checkpoint(
                schedule_id, occurrence_key, status, fired_at, consumed_at
            ) VALUES ('daily', 'previous', 'FIRED', ?, ?)
            """,
            ((clock.now() - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
             clock.now().isoformat().replace("+00:00", "Z")),
        )
    blocked = await scheduler.tick()
    clock.advance(61)
    allowed = await scheduler.tick()
    assert blocked.triggered == 0
    assert allowed.triggered == 1
    assert len(await database.fetch_all("SELECT * FROM outbox_event")) == 1
    await database.close()


@pytest.mark.asyncio
async def test_missed_policy_skip_records_checkpoint_without_event(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 26, 1))
    database, scheduler = await setup(
        tmp_path,
        clock,
        ScheduleSpec("skip", time(9, 25), window_seconds=30, missed_policy=MissedTriggerPolicy.SKIP),
    )
    result = await scheduler.tick()
    assert result.skipped == 1 and result.triggered == 0
    assert await database.fetch_one("SELECT status FROM schedule_checkpoint") is not None
    assert await database.fetch_one("SELECT * FROM outbox_event") is None
    await database.close()


@pytest.mark.asyncio
async def test_missed_fire_once_marks_event_and_expiry(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 26, 1))
    database, scheduler = await setup(
        tmp_path,
        clock,
        ScheduleSpec(
            "recover",
            time(9, 25),
            window_seconds=30,
            missed_policy=MissedTriggerPolicy.FIRE_ONCE,
            max_missed_seconds=120,
        ),
    )
    result = await scheduler.tick()
    row = await database.fetch_one("SELECT envelope_json FROM outbox_event")
    assert result.triggered == 1 and row is not None
    assert '"missed":true' in row["envelope_json"]
    await database.close()


@pytest.mark.asyncio
async def test_missed_trigger_beyond_limit_is_expired(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 30))
    database, scheduler = await setup(
        tmp_path,
        clock,
        ScheduleSpec("expired", time(9, 25), window_seconds=30, max_missed_seconds=60),
    )
    result = await scheduler.tick()
    row = await database.fetch_one("SELECT status FROM schedule_checkpoint")
    assert result.expired == 1 and row is not None and row["status"] == "EXPIRED"
    await database.close()


@pytest.mark.asyncio
async def test_non_trading_day_is_skipped_and_restart_does_not_duplicate(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 25))
    database = SQLiteDatabase(tmp_path / "holiday.db")
    await database.initialize()
    calendar = Calendar({datetime.fromisoformat("2026-08-17").date()})
    spec = ScheduleSpec("holiday", time(9, 25), trading_days_only=True)
    scheduler = Scheduler(
        database, clock, Uuid7Generator(clock, random_bits=lambda _: 1), [spec], calendar=calendar
    )
    first = await scheduler.tick()
    restarted = Scheduler(
        database, clock, Uuid7Generator(clock, random_bits=lambda _: 2), [spec], calendar=calendar
    )
    second = await restarted.tick()
    assert first.skipped == 1 and second.skipped == 0
    assert len(await database.fetch_all("SELECT * FROM schedule_checkpoint")) == 1
    await database.close()


@pytest.mark.asyncio
async def test_before_window_is_deferred_and_service_lifecycle(tmp_path: Path) -> None:
    clock = FakeClock(at(9, 24))
    database, scheduler = await setup(tmp_path, clock, ScheduleSpec("later", time(9, 25)))
    assert (await scheduler.tick()).deferred == 1
    await scheduler.start()
    await scheduler.checkpoint()
    await scheduler.quiesce()
    await scheduler.stop()
    assert await database.fetch_one("SELECT * FROM outbox_event") is None
    await database.close()


def test_schedule_configuration_validation() -> None:
    with pytest.raises(ValueError):
        ScheduleSpec("", time(9))
    with pytest.raises(ValueError):
        ScheduleSpec("x", time(9), window_seconds=0)
    with pytest.raises(ValueError):
        ScheduleSpec("x", time(9), cooldown_seconds=-1)
    with pytest.raises(ValueError):
        ScheduleSpec("x", time(9), max_missed_seconds=-1)
    with pytest.raises(ValueError):
        ScheduleSpec("x", time(9), priority=101)
    with pytest.raises(ValueError):
        ScheduleSpec("x", time(9), timezone="Bad/Zone")
