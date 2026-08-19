"""Persistent subscription, rate-limit, deduplication and read-state delivery."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivery_id: str | None
    status: str


class InsightDeliveryService:
    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database, self._clock = database, clock

    async def subscribe(
        self, subscription_id: str, *, topic: str = "market_summary", minimum_level: str = "INFO",
        channel: str = "local", quiet_start_hour: int | None = None,
        quiet_end_hour: int | None = None, hourly_limit: int = 10,
    ) -> None:
        if not subscription_id or minimum_level not in {"INFO", "WARNING", "ERROR"}:
            raise ValueError("subscription and level are invalid")
        if hourly_limit < 1 or (quiet_start_hour is None) != (quiet_end_hour is None):
            raise ValueError("hourly limit and quiet hours are invalid")
        now = _time(self._clock.now())
        async with self._database.transaction() as tx:
            await tx.execute(
                """INSERT INTO insight_subscription VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(subscription_id) DO UPDATE SET topic=excluded.topic,
                   minimum_level=excluded.minimum_level, channel=excluded.channel,
                   quiet_start_hour=excluded.quiet_start_hour,
                   quiet_end_hour=excluded.quiet_end_hour, hourly_limit=excluded.hourly_limit,
                   enabled=1""",
                (subscription_id, topic, minimum_level, channel, quiet_start_hour,
                 quiet_end_hour, hourly_limit, now),
            )

    async def deliver(self, subscription_id: str, insight_id: str, *, level: str = "INFO") -> DeliveryResult:
        row = await self._database.fetch_one(
            "SELECT * FROM insight_subscription WHERE subscription_id = ? AND enabled = 1",
            (subscription_id,),
        )
        if row is None:
            return DeliveryResult(None, "NOT_SUBSCRIBED")
        channel = str(row["channel"])
        existing = await self._database.fetch_one(
            "SELECT delivery_id FROM insight_delivery WHERE subscription_id = ? AND insight_id = ? AND channel = ?",
            (subscription_id, insight_id, channel),
        )
        if existing is not None:
            return DeliveryResult(str(existing["delivery_id"]), "DUPLICATE")
        if _rank(level) < _rank(str(row["minimum_level"])):
            return DeliveryResult(None, "FILTERED")
        now = self._clock.now().astimezone(UTC)
        start, end = row["quiet_start_hour"], row["quiet_end_hour"]
        if start is not None and _quiet(now.hour, int(start), int(end)):
            return DeliveryResult(None, "QUIET")
        count = await self._database.fetch_one(
            "SELECT count(*) AS value FROM insight_delivery WHERE subscription_id = ? AND delivered_at >= ?",
            (subscription_id, _time(now - timedelta(hours=1))),
        )
        if count is not None and int(count["value"]) >= int(row["hourly_limit"]):
            return DeliveryResult(None, "RATE_LIMITED")
        delivery_id = hashlib.sha256(f"{subscription_id}:{insight_id}:{channel}".encode()).hexdigest()[:24]
        try:
            async with self._database.transaction() as tx:
                await tx.execute(
                    "INSERT INTO insight_delivery VALUES (?, ?, ?, ?, ?, NULL)",
                    (delivery_id, subscription_id, insight_id, channel, _time(now)),
                )
        except sqlite3.IntegrityError:
            return DeliveryResult(delivery_id, "DUPLICATE")
        return DeliveryResult(delivery_id, "DELIVERED")

    async def mark_read(self, delivery_id: str) -> bool:
        async with self._database.transaction() as tx:
            cursor = await tx.execute(
                "UPDATE insight_delivery SET read_at = COALESCE(read_at, ?) WHERE delivery_id = ?",
                (_time(self._clock.now()), delivery_id),
            )
        return cursor.rowcount == 1

    async def deliveries(self, subscription_id: str) -> tuple[dict[str, object], ...]:
        rows = await self._database.fetch_all(
            "SELECT * FROM insight_delivery WHERE subscription_id = ? ORDER BY delivered_at DESC, delivery_id",
            (subscription_id,),
        )
        return tuple(dict(row) for row in rows)


def _rank(level: str) -> int:
    try:
        return {"INFO": 0, "WARNING": 1, "ERROR": 2}[level]
    except KeyError as error:
        raise ValueError("delivery level is invalid") from error


def _quiet(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else hour >= start or hour < end


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
