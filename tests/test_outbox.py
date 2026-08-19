from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.events import (
    DeliveryOutcome,
    EventBus,
    JsonEventCodec,
    OutboxRelay,
    OutboxRetryPolicy,
    OutboxWriter,
    OverflowPolicy,
    PersistedBusMessage,
    PublishReport,
    SubscriptionConfig,
    TransactionalInboxConsumer,
)
from active_agent_platform.events.models import DeliveryResult
from active_agent_platform.foundation import Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import BusMessage


@dataclass(frozen=True, slots=True)
class Event:
    msg_id: str
    msg_type: str = "fact.created"
    source: str = "test.source"
    priority: int = 60
    correlation_id: str = "correlation-1"
    causation_id: str | None = "cause-1"
    dedup_key: str | None = None
    payload: dict[str, object] | None = None


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 17, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - datetime(2026, 8, 17, tzinfo=UTC)).total_seconds()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FailingPublisher:
    def __init__(self, failures: int, *, rejected: bool = False) -> None:
        self.failures = failures
        self.rejected = rejected
        self.messages: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> PublishReport:
        self.messages.append(message)
        if len(self.messages) <= self.failures:
            raise RuntimeError("transport unavailable")
        outcome = DeliveryOutcome.REJECTED if self.rejected else DeliveryOutcome.ENQUEUED
        return PublishReport(
            message.msg_id, (DeliveryResult("target", outcome),)
        )


async def setup(tmp_path: Path) -> tuple[SQLiteDatabase, Clock, OutboxWriter]:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    clock = Clock()
    return database, clock, OutboxWriter(clock)


async def append(
    database: SQLiteDatabase, writer: OutboxWriter, event: Event
) -> None:
    async with database.transaction() as transaction:
        await writer.append(transaction, event)


@pytest.mark.asyncio
async def test_domain_fact_and_outbox_append_commit_atomically(tmp_path: Path) -> None:
    database, _, writer = await setup(tmp_path)
    event = Event("event-1", payload={"value": 1})

    async with database.transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO artifact(
                artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
            ) VALUES ('fact-1', 'local://fact-1', 'digest', 1, 'text/plain', 'now', ?)
            """,
            (event.correlation_id,),
        )
        await writer.append(transaction, event)

    fact = await database.fetch_one("SELECT * FROM artifact WHERE artifact_id = 'fact-1'")
    outbox = await database.fetch_one(
        "SELECT event_id, publish_state, attempt FROM outbox_event"
    )
    assert fact is not None
    assert outbox is not None and tuple(outbox) == ("event-1", "PENDING", 0)
    await database.close()


@pytest.mark.asyncio
async def test_inbox_fact_and_outbox_form_one_idempotent_transaction(tmp_path: Path) -> None:
    database, clock, writer = await setup(tmp_path)
    consumer = TransactionalInboxConsumer(
        "projector",
        database,
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
    )
    source = Event("source-1", correlation_id="c1", dedup_key="source-action:1")

    async def project(transaction: SQLiteTransaction, _: Event) -> None:
        await transaction.execute(
            """
            INSERT INTO artifact(
                artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
            ) VALUES ('fact-1', 'local://fact-1', 'digest', 1, 'text/plain', 'now', 'c1')
            """
        )
        await writer.append(
            transaction,
            Event("derived-1", correlation_id="c1", causation_id=source.msg_id),
        )

    first = await consumer.consume(source, project)
    duplicate = await consumer.consume(source, project)

    assert first.outcome.value == "PROCESSED"
    assert duplicate.outcome.value == "DUPLICATE"
    assert len(await database.fetch_all("SELECT * FROM artifact")) == 1
    assert len(await database.fetch_all("SELECT * FROM outbox_event")) == 1
    await database.close()


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_fact_and_outbox(tmp_path: Path) -> None:
    database, _, writer = await setup(tmp_path)
    with pytest.raises(RuntimeError, match="crash before commit"):
        async with database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO artifact(
                    artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
                ) VALUES ('fact-1', 'local://fact-1', 'digest', 1, 'text/plain', 'now', 'c1')
                """
            )
            await writer.append(transaction, Event("event-1"))
            raise RuntimeError("crash before commit")

    assert await database.fetch_one("SELECT * FROM artifact") is None
    assert await database.fetch_one("SELECT * FROM outbox_event") is None
    await database.close()


@pytest.mark.asyncio
async def test_relay_publishes_original_envelope_and_confirms(tmp_path: Path) -> None:
    database, clock, writer = await setup(tmp_path)
    event = Event("event-1", payload={"nested": [1, 2]})
    await append(database, writer, event)
    bus = EventBus()
    subscription = bus.subscribe(
        SubscriptionConfig(
            "consumer", frozenset({"fact.created"}), 10, OverflowPolicy.REJECT
        )
    )
    await bus.start()
    relay = OutboxRelay(database, bus, clock)

    result = await relay.publish_due()
    received = await subscription.get()

    row = await database.fetch_one(
        "SELECT publish_state, attempt, published_at, next_attempt_at FROM outbox_event"
    )
    assert result == result.__class__(selected=1, published=1, deferred=0)
    assert isinstance(received, PersistedBusMessage)
    assert received.msg_id == event.msg_id
    assert received.msg_type == event.msg_type
    assert received.source == event.source
    assert received.priority == event.priority
    assert received.envelope["correlation_id"] == event.correlation_id
    assert received.envelope["causation_id"] == event.causation_id
    assert received.envelope["payload"] == event.payload
    assert row is not None and tuple(row)[:2] == ("PUBLISHED", 1)
    assert row["published_at"] is not None and row["next_attempt_at"] is None
    assert (await relay.publish_due()).selected == 0
    await bus.stop()
    await database.close()


@pytest.mark.asyncio
async def test_failure_is_deferred_until_due_with_capped_exponential_backoff(
    tmp_path: Path,
) -> None:
    database, clock, writer = await setup(tmp_path)
    await append(database, writer, Event("event-1"))
    publisher = FailingPublisher(2)
    relay = OutboxRelay(
        database,
        publisher,
        clock,
        retry_policy=OutboxRetryPolicy(2, 3, 5),
    )

    first = await relay.publish_due()
    not_due = await relay.publish_due()
    clock.advance(2)
    second = await relay.publish_due()
    clock.advance(4.9)
    still_not_due = await relay.publish_due()
    clock.advance(0.1)
    third = await relay.publish_due()

    row = await database.fetch_one(
        "SELECT publish_state, attempt, next_attempt_at FROM outbox_event"
    )
    assert first.deferred == 1 and not_due.selected == 0
    assert second.deferred == 1 and still_not_due.selected == 0
    assert third.published == 1
    assert len(publisher.messages) == 3
    assert all(message.msg_id == "event-1" for message in publisher.messages)
    assert row is not None and tuple(row) == ("PUBLISHED", 3, None)
    await database.close()


@pytest.mark.asyncio
async def test_rejected_delivery_is_not_acknowledged(tmp_path: Path) -> None:
    database, clock, writer = await setup(tmp_path)
    await append(database, writer, Event("event-1"))
    publisher = FailingPublisher(0, rejected=True)
    relay = OutboxRelay(database, publisher, clock)

    result = await relay.publish_due()

    row = await database.fetch_one("SELECT publish_state, attempt FROM outbox_event")
    assert result.deferred == 1
    assert row is not None and tuple(row) == ("PENDING", 1)
    await database.close()


@pytest.mark.asyncio
async def test_restart_recovers_pending_rows_in_stable_batches(tmp_path: Path) -> None:
    database, clock, writer = await setup(tmp_path)
    for event_id in ("event-1", "event-2", "event-3"):
        await append(database, writer, Event(event_id))
    publisher = FailingPublisher(0)
    first_relay = OutboxRelay(
        database, publisher, clock, batch_size=2
    )
    first = await first_relay.publish_due()

    restarted = OutboxRelay(database, publisher, clock)
    second = await restarted.publish_due()

    assert first.selected == first.published == 2
    assert second.selected == second.published == 1
    assert [message.msg_id for message in publisher.messages] == [
        "event-1",
        "event-2",
        "event-3",
    ]
    await database.close()


@pytest.mark.asyncio
async def test_publish_before_confirmation_crash_causes_safe_redelivery(
    tmp_path: Path,
) -> None:
    database, clock, writer = await setup(tmp_path)
    await append(database, writer, Event("event-1"))
    publisher = FailingPublisher(0)

    class CrashBeforeConfirmRelay(OutboxRelay):
        async def _confirm(self, event_id: str, attempt: int) -> None:
            raise RuntimeError(f"crash before confirming {event_id}:{attempt}")

    crashing = CrashBeforeConfirmRelay(
        database, publisher, clock
    )
    with pytest.raises(RuntimeError, match="crash before confirming"):
        await crashing.publish_due()
    pending = await database.fetch_one("SELECT publish_state, attempt FROM outbox_event")

    recovered = OutboxRelay(database, publisher, clock)
    result = await recovered.publish_due()

    assert pending is not None and tuple(pending) == ("PENDING", 0)
    assert result.published == 1
    assert len(publisher.messages) == 2
    await database.close()


@pytest.mark.asyncio
async def test_lifecycle_checkpoint_and_quiesce(tmp_path: Path) -> None:
    database, clock, writer = await setup(tmp_path)
    await append(database, writer, Event("event-1"))
    publisher = FailingPublisher(0)
    relay = OutboxRelay(database, publisher, clock)

    await relay.start()
    await relay.checkpoint()
    await relay.quiesce()
    await relay.checkpoint()
    await relay.stop()

    assert len(publisher.messages) == 1
    await database.close()


def test_codec_and_configuration_validation() -> None:
    codec = JsonEventCodec()
    with pytest.raises(TypeError, match="JSON object"):
        codec.decode("[]")
    with pytest.raises(ValueError, match="transport fields"):
        codec.decode('{"msg_id":"only"}')
    with pytest.raises(ValueError):
        OutboxRetryPolicy(initial_delay_seconds=-1)
    with pytest.raises(ValueError):
        OutboxRetryPolicy(multiplier=0)
    with pytest.raises(ValueError):
        OutboxRetryPolicy(max_delay_seconds=-1)
    with pytest.raises(ValueError):
        OutboxRetryPolicy().delay_after(0)


@pytest.mark.asyncio
async def test_writer_rejects_missing_correlation_and_oversized_event(tmp_path: Path) -> None:
    database, _, writer = await setup(tmp_path)

    @dataclass(frozen=True)
    class MissingCorrelation:
        msg_id: str = "missing"
        msg_type: str = "fact.created"
        source: str = "source"
        priority: int = 1

    async with database.transaction() as transaction:
        with pytest.raises(ValueError, match="correlation_id"):
            await writer.append(transaction, MissingCorrelation())
        with pytest.raises(ValueError, match="64 KiB"):
            await writer.append(transaction, Event("large", payload={"x": "x" * 65536}))
    await database.close()


@pytest.mark.asyncio
async def test_relay_configuration_validation(tmp_path: Path) -> None:
    database, clock, _ = await setup(tmp_path)
    publisher = FailingPublisher(0)
    with pytest.raises(ValueError, match="batch_size"):
        OutboxRelay(database, publisher, clock, batch_size=0)
    with pytest.raises(ValueError, match="poll_interval"):
        OutboxRelay(
            database, publisher, clock, poll_interval_seconds=0
        )
    await database.close()
