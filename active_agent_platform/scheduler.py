"""Persistent, virtual-clock-friendly schedule window service."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from active_agent_platform.events import EventEnvelope, OutboxWriter
from active_agent_platform.state import TradingCalendar
from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock, UuidGenerator


class MissedTriggerPolicy(StrEnum):
    SKIP = "SKIP"
    FIRE_ONCE = "FIRE_ONCE"


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    schedule_id: str
    at: time
    window_seconds: float = 60.0
    cooldown_seconds: float = 0.0
    missed_policy: MissedTriggerPolicy = MissedTriggerPolicy.SKIP
    max_missed_seconds: float = 300.0
    timezone: str = "Asia/Shanghai"
    trading_days_only: bool = False
    priority: int = 50
    payload_data: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.schedule_id:
            raise ValueError("schedule_id must not be empty")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if self.max_missed_seconds < 0:
            raise ValueError("max_missed_seconds must be non-negative")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA zone") from error


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    evaluated: int
    triggered: int
    skipped: int
    expired: int
    deferred: int


class Scheduler:
    name = "scheduler"

    def __init__(
        self,
        database: SQLiteDatabase,
        clock: Clock,
        uuid: UuidGenerator,
        schedules: Iterable[ScheduleSpec] = (),
        *,
        calendar: TradingCalendar | None = None,
        outbox: OutboxWriter | None = None,
        source: str = "scheduler",
        poll_interval_seconds: float = 1.0,
    ) -> None:
        values = tuple(schedules)
        ids = [item.schedule_id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("schedule IDs must be unique")
        if not source:
            raise ValueError("source must not be empty")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._database = database
        self._clock = clock
        self._uuid = uuid
        self._schedules = values
        self._calendar = calendar or TradingCalendar()
        self._outbox = outbox or OutboxWriter(clock)
        self._source = source
        self._poll_interval = poll_interval_seconds
        self._stopping = False
        self._accepting = False

    async def start(self) -> None:
        self._stopping = False
        self._accepting = True

    async def serve(self) -> None:
        while not self._stopping:
            await self.tick()
            await self._clock.sleep(self._poll_interval)

    async def quiesce(self) -> None:
        self._accepting = False

    async def checkpoint(self) -> None:
        await self.tick()

    async def stop(self) -> None:
        self._accepting = False
        self._stopping = True

    async def tick(self) -> SchedulerResult:
        now = self._clock.now()
        evaluated = triggered = skipped = expired = deferred = 0
        for spec in self._schedules:
            evaluated += 1
            occurrence = self._occurrence(spec, now)
            if occurrence is None:
                deferred += 1
                continue
            occurrence_key, scheduled_at, age = occurrence
            if spec.trading_days_only and not self._calendar.is_trading_day(
                scheduled_at.astimezone(ZoneInfo(spec.timezone)).date()
            ):
                if await self._checkpoint(spec.schedule_id, occurrence_key, "SKIPPED"):
                    skipped += 1
                continue
            if age <= spec.window_seconds:
                if await self._fire(spec, occurrence_key, scheduled_at, now, missed=False):
                    triggered += 1
                else:
                    deferred += 1
            elif age <= spec.window_seconds + spec.max_missed_seconds:
                if spec.missed_policy is MissedTriggerPolicy.FIRE_ONCE:
                    if await self._fire(spec, occurrence_key, scheduled_at, now, missed=True):
                        triggered += 1
                    else:
                        deferred += 1
                elif await self._checkpoint(spec.schedule_id, occurrence_key, "SKIPPED"):
                    skipped += 1
            elif await self._checkpoint(spec.schedule_id, occurrence_key, "EXPIRED"):
                expired += 1
        return SchedulerResult(evaluated, triggered, skipped, expired, deferred)

    def _occurrence(
        self, spec: ScheduleSpec, now: datetime
    ) -> tuple[str, datetime, float] | None:
        local = now.astimezone(ZoneInfo(spec.timezone))
        scheduled_local = datetime.combine(local.date(), spec.at, tzinfo=local.tzinfo)
        age = (local - scheduled_local).total_seconds()
        if age < 0:
            return None
        scheduled_utc = scheduled_local.astimezone(UTC)
        return f"{local.date().isoformat()}T{spec.at.isoformat()}", scheduled_utc, age

    async def _fire(
        self,
        spec: ScheduleSpec,
        occurrence_key: str,
        scheduled_at: datetime,
        now: datetime,
        *,
        missed: bool,
    ) -> bool:
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT status FROM schedule_checkpoint WHERE schedule_id = ? AND occurrence_key = ?",
                (spec.schedule_id, occurrence_key),
            )
            if existing is not None:
                return False
            if spec.cooldown_seconds > 0:
                previous = await transaction.fetch_one(
                    """
                    SELECT fired_at FROM schedule_checkpoint
                    WHERE schedule_id = ? AND status = 'FIRED' AND fired_at IS NOT NULL
                    ORDER BY fired_at DESC LIMIT 1
                    """,
                    (spec.schedule_id,),
                )
                if previous is not None:
                    fired_at = datetime.fromisoformat(str(previous["fired_at"]))
                    if (now.astimezone(UTC) - fired_at.astimezone(UTC)).total_seconds() < spec.cooldown_seconds:
                        return False
            msg_id = str(self._uuid.new())
            payload = {
                "event_type": "schedule.triggered",
                "stimulus_id": occurrence_key,
                "data": {
                    "schedule_id": spec.schedule_id,
                    "occurrence_key": occurrence_key,
                    "scheduled_at": _timestamp(scheduled_at),
                    "triggered_at": _timestamp(now),
                    "missed": missed,
                    **(spec.payload_data or {}),
                },
                "data_quality": "VALID",
            }
            event = EventEnvelope(
                msg_id=msg_id,
                msg_type="schedule.triggered",
                source=self._source,
                occurred_at=scheduled_at,
                published_at=now,
                priority=spec.priority,
                correlation_id=msg_id,
                dedup_key=f"{spec.schedule_id}:{occurrence_key}",
                payload=payload,
                expires_at=(now + timedelta(seconds=spec.max_missed_seconds)) if missed else scheduled_at + timedelta(seconds=spec.window_seconds),
            )
            await self._outbox.append(transaction, event)
            await transaction.execute(
                """
                INSERT INTO schedule_checkpoint(
                    schedule_id, occurrence_key, status, fired_at, consumed_at
                ) VALUES (?, ?, 'FIRED', ?, ?)
                """,
                (spec.schedule_id, occurrence_key, _timestamp(now), _timestamp(now)),
            )
        return True

    async def _checkpoint(self, schedule_id: str, occurrence_key: str, status: str) -> bool:
        async with self._database.transaction() as transaction:
            cursor = await transaction.execute(
                """
                INSERT OR IGNORE INTO schedule_checkpoint(
                    schedule_id, occurrence_key, status, consumed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (schedule_id, occurrence_key, status, _timestamp(self._clock.now())),
            )
            return cursor.rowcount == 1


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
