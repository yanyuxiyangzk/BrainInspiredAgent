"""Transactional Outbox writer and restart-safe event relay."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast

from active_agent_platform.events.contracts import EventSchemaRegistry
from active_agent_platform.events.models import DeliveryOutcome, PublishReport
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import BusMessage, Clock


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True, slots=True)
class OutboxRetryPolicy:
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least one")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be non-negative")

    def delay_after(self, failed_attempt: int) -> float:
        if failed_attempt < 1:
            raise ValueError("failed_attempt must be positive")
        delay = self.initial_delay_seconds * self.multiplier ** (failed_attempt - 1)
        return min(delay, self.max_delay_seconds)


@dataclass(frozen=True, slots=True)
class PersistedBusMessage:
    """Domain-neutral decoded envelope passed from durable storage to EventBus."""

    msg_id: str
    msg_type: str
    source: str
    priority: int
    envelope: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "envelope", MappingProxyType(dict(self.envelope)))


class EventCodec(Protocol):
    def encode(self, message: BusMessage) -> str: ...

    def decode(self, envelope_json: str) -> BusMessage: ...


class JsonEventCodec:
    def encode(self, message: BusMessage) -> str:
        value: object
        to_dict = getattr(message, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
        elif is_dataclass(message):
            value = asdict(cast(Any, message))
        elif isinstance(message, Mapping):
            value = dict(message)
        elif hasattr(message, "__dict__"):
            value = vars(message)
        else:
            raise TypeError("message requires a custom EventCodec")
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)

    def decode(self, envelope_json: str) -> BusMessage:
        value = json.loads(envelope_json)
        if not isinstance(value, dict):
            raise TypeError("event envelope must be a JSON object")
        try:
            msg_id = str(value["msg_id"])
            msg_type = str(value["msg_type"])
            source = str(value["source"])
            priority = int(value["priority"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("event envelope has invalid transport fields") from error
        return PersistedBusMessage(msg_id, msg_type, source, priority, value)


class EventPublisher(Protocol):
    async def publish(self, message: BusMessage) -> PublishReport: ...


@dataclass(frozen=True, slots=True)
class RelayBatchResult:
    selected: int
    published: int
    deferred: int


class OutboxWriter:
    """Append immutable events using the caller's domain transaction."""

    def __init__(
        self,
        clock: Clock,
        codec: EventCodec | None = None,
        *,
        schema_registry: EventSchemaRegistry | None = None,
    ) -> None:
        self._clock = clock
        self._codec = codec or JsonEventCodec()
        self._schema_registry = schema_registry

    async def append(self, transaction: SQLiteTransaction, message: BusMessage) -> None:
        if self._schema_registry is not None:
            self._schema_registry.validate(message)  # type: ignore[arg-type]
        envelope_json = self._codec.encode(message)
        if len(envelope_json.encode("utf-8")) > 65536:
            raise ValueError("serialized event must not exceed 64 KiB")
        correlation_id = _field(envelope_json, "correlation_id")
        await transaction.execute(
            """
            INSERT INTO outbox_event(
                event_id, msg_type, envelope_json, publish_state, attempt,
                next_attempt_at, created_at, correlation_id
            ) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
            """,
            (
                message.msg_id,
                message.msg_type,
                envelope_json,
                OutboxStatus.PENDING,
                _timestamp(self._clock.now()),
                correlation_id,
            ),
        )


class OutboxRelay:
    """Publish due Outbox rows and persist acknowledgement or retry schedule."""

    name = "outbox_relay"

    def __init__(
        self,
        database: SQLiteDatabase,
        publisher: EventPublisher,
        clock: Clock,
        *,
        codec: EventCodec | None = None,
        retry_policy: OutboxRetryPolicy | None = None,
        batch_size: int = 100,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._database = database
        self._publisher = publisher
        self._clock = clock
        self._codec = codec or JsonEventCodec()
        self._retry_policy = retry_policy or OutboxRetryPolicy()
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._stopping = asyncio.Event()
        self._accepting = False

    async def start(self) -> None:
        self._stopping = asyncio.Event()
        self._accepting = True

    async def serve(self) -> None:
        while not self._stopping.is_set():
            await self.publish_due()
            await self._clock.sleep(self._poll_interval_seconds)

    async def quiesce(self) -> None:
        self._accepting = False

    async def checkpoint(self) -> None:
        if self._accepting:
            await self.publish_due()

    async def stop(self) -> None:
        self._accepting = False
        self._stopping.set()

    async def publish_due(self) -> RelayBatchResult:
        rows = await self._database.fetch_all(
            """
            SELECT event_id, envelope_json, attempt
            FROM outbox_event
            WHERE publish_state = ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at, event_id
            LIMIT ?
            """,
            (OutboxStatus.PENDING, _timestamp(self._clock.now()), self._batch_size),
        )
        published = 0
        deferred = 0
        for row in rows:
            event_id = str(row["event_id"])
            attempt = int(row["attempt"]) + 1
            try:
                message = self._codec.decode(str(row["envelope_json"]))
                report = await self._publisher.publish(message)
                if not _acknowledged(report):
                    raise RuntimeError("event delivery was not acknowledged")
            except Exception:  # noqa: BLE001 - transport/codec failures remain recoverable
                await self._defer(event_id, attempt)
                deferred += 1
            else:
                await self._confirm(event_id, attempt)
                published += 1
        return RelayBatchResult(len(rows), published, deferred)

    async def _confirm(self, event_id: str, attempt: int) -> None:
        async with self._database.transaction() as transaction:
            await transaction.execute(
                """
                UPDATE outbox_event
                SET publish_state = ?, attempt = ?, published_at = ?, next_attempt_at = NULL
                WHERE event_id = ? AND publish_state = ?
                """,
                (
                    OutboxStatus.PUBLISHED,
                    attempt,
                    _timestamp(self._clock.now()),
                    event_id,
                    OutboxStatus.PENDING,
                ),
            )

    async def _defer(self, event_id: str, attempt: int) -> None:
        next_attempt = self._clock.now().timestamp() + self._retry_policy.delay_after(attempt)
        next_attempt_at = datetime.fromtimestamp(next_attempt, UTC)
        async with self._database.transaction() as transaction:
            await transaction.execute(
                """
                UPDATE outbox_event
                SET attempt = ?, next_attempt_at = ?
                WHERE event_id = ? AND publish_state = ?
                """,
                (
                    attempt,
                    _timestamp(next_attempt_at),
                    event_id,
                    OutboxStatus.PENDING,
                ),
            )


def _acknowledged(report: PublishReport) -> bool:
    failures = {DeliveryOutcome.REJECTED, DeliveryOutcome.DROPPED, DeliveryOutcome.CLOSED}
    return all(delivery.outcome not in failures for delivery in report.deliveries)


def _field(envelope_json: str, name: str) -> str:
    value = json.loads(envelope_json)
    if not isinstance(value, dict) or not value.get(name):
        raise ValueError(f"event envelope requires {name}")
    return str(value[name])


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
