from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.events import (
    ConsumptionOutcome,
    RetryableConsumptionError,
    RetryPolicy,
    TransactionalInboxConsumer,
)
from active_agent_platform.foundation import Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction


@dataclass(frozen=True, slots=True)
class Message:
    msg_id: str
    correlation_id: str
    dedup_key: str | None = None
    payload: str = "payload"


class RecordingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 17, tzinfo=UTC)
        self.delays: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return sum(self.delays)

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.current += timedelta(seconds=seconds)


async def inbox(
    tmp_path: Path,
    *,
    consumer_id: str = "consumer",
    retry_policy: RetryPolicy | None = None,
) -> tuple[SQLiteDatabase, RecordingClock, TransactionalInboxConsumer]:
    database = SQLiteDatabase(tmp_path / f"{consumer_id}.db")
    await database.initialize()
    clock = RecordingClock()
    consumer = TransactionalInboxConsumer(
        consumer_id,
        database,
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        retry_policy=retry_policy,
    )
    return database, clock, consumer


async def write_artifact(
    transaction: SQLiteTransaction, message: Message
) -> None:
    await transaction.execute(
        """
        INSERT INTO artifact(
            artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
        ) VALUES (?, ?, ?, 1, 'text/plain', 'now', ?)
        """,
        (message.msg_id, f"local://{message.msg_id}", message.payload, message.correlation_id),
    )


@pytest.mark.asyncio
async def test_success_commits_inbox_and_business_effect_atomically(tmp_path: Path) -> None:
    database, _, consumer = await inbox(tmp_path)
    message = Message("m1", "c1", "business:1")

    result = await consumer.consume(message, write_artifact)

    row = await database.fetch_one(
        "SELECT status, attempt, processed_at FROM inbox_message WHERE msg_id = 'm1'"
    )
    artifact = await database.fetch_one("SELECT digest FROM artifact WHERE artifact_id = 'm1'")
    assert result.outcome is ConsumptionOutcome.PROCESSED
    assert result.attempts == 1
    assert row is not None and tuple(row)[:2] == ("DONE", 1)
    assert row["processed_at"] is not None
    assert artifact is not None and artifact["digest"] == "payload"
    await database.close()


@pytest.mark.asyncio
async def test_duplicate_message_and_business_key_do_not_repeat_effect(tmp_path: Path) -> None:
    database, _, consumer = await inbox(tmp_path)
    calls = 0

    async def count_and_write(transaction: SQLiteTransaction, message: Message) -> None:
        nonlocal calls
        calls += 1
        await write_artifact(transaction, message)

    first = await consumer.consume(Message("m1", "c1", "business:1"), count_and_write)
    same_message = await consumer.consume(
        Message("m1", "c1", "business:1"), count_and_write
    )
    same_business_action = await consumer.consume(
        Message("m2", "c1", "business:1"), count_and_write
    )

    assert first.outcome is ConsumptionOutcome.PROCESSED
    assert same_message.outcome is ConsumptionOutcome.DUPLICATE
    assert same_business_action.outcome is ConsumptionOutcome.DUPLICATE
    assert calls == 1
    assert len(await database.fetch_all("SELECT * FROM inbox_message")) == 1
    await database.close()


@pytest.mark.asyncio
async def test_retryable_failures_persist_attempts_then_succeed(tmp_path: Path) -> None:
    database, clock, consumer = await inbox(
        tmp_path,
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=2,
            multiplier=3,
            max_delay_seconds=5,
        ),
    )
    calls = 0

    async def flaky(transaction: SQLiteTransaction, message: Message) -> None:
        nonlocal calls
        calls += 1
        await write_artifact(transaction, message)
        if calls < 3:
            raise RetryableConsumptionError("temporary")

    result = await consumer.consume(Message("m1", "c1"), flaky)

    row = await database.fetch_one(
        "SELECT status, attempt, error_id FROM inbox_message WHERE msg_id = 'm1'"
    )
    assert result == result.__class__(ConsumptionOutcome.PROCESSED, 3)
    assert calls == 3
    assert clock.delays == [2, 5]
    assert row is not None and tuple(row) == ("DONE", 3, None)
    assert len(await database.fetch_all("SELECT * FROM artifact")) == 1
    assert await database.fetch_one("SELECT * FROM dead_letter") is None
    await database.close()


@pytest.mark.asyncio
async def test_nonretryable_failure_rolls_back_effect_and_enters_dead_letter(
    tmp_path: Path,
) -> None:
    database, clock, consumer = await inbox(tmp_path)

    async def broken(transaction: SQLiteTransaction, message: Message) -> None:
        await write_artifact(transaction, message)
        raise ValueError("invalid payload")

    result = await consumer.consume(Message("m1", "c1"), broken)

    inbox_row = await database.fetch_one(
        "SELECT status, attempt, error_id FROM inbox_message WHERE msg_id = 'm1'"
    )
    dead = await database.fetch_one(
        "SELECT consumer_id, msg_id, envelope_json, error_id FROM dead_letter"
    )
    assert result.outcome is ConsumptionOutcome.DEAD_LETTERED
    assert result.attempts == 1
    assert clock.delays == []
    assert inbox_row is not None and inbox_row["status"] == "DEAD_LETTER"
    assert inbox_row["error_id"] == result.error_id
    assert dead is not None and dead["msg_id"] == "m1"
    assert dead["error_id"] == result.error_id
    assert '"payload":"payload"' in dead["envelope_json"]
    assert await database.fetch_one("SELECT * FROM artifact") is None
    await database.close()


@pytest.mark.asyncio
async def test_retry_exhaustion_enters_one_dead_letter_and_redelivery_is_duplicate(
    tmp_path: Path,
) -> None:
    database, clock, consumer = await inbox(
        tmp_path,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=1),
    )
    calls = 0

    async def unavailable(_: SQLiteTransaction, __: Message) -> None:
        nonlocal calls
        calls += 1
        raise RetryableConsumptionError("offline")

    message = Message("m1", "c1")
    result = await consumer.consume(message, unavailable)
    duplicate = await consumer.consume(message, unavailable)

    assert result.outcome is ConsumptionOutcome.DEAD_LETTERED
    assert result.attempts == 3
    assert duplicate.outcome is ConsumptionOutcome.DUPLICATE
    assert calls == 3
    assert clock.delays == [1, 2]
    assert len(await database.fetch_all("SELECT * FROM dead_letter")) == 1
    await database.close()


@pytest.mark.asyncio
async def test_retry_state_survives_consumer_recreation(tmp_path: Path) -> None:
    database, clock, first = await inbox(
        tmp_path,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0),
    )
    await first._record_retry(Message("m1", "c1"), 1, "old-error")
    second = TransactionalInboxConsumer(
        "consumer",
        database,
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 2),
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0),
    )

    result = await second.consume(Message("m1", "c1"), write_artifact)

    assert result.attempts == 2
    row = await database.fetch_one("SELECT status, attempt FROM inbox_message")
    assert row is not None and tuple(row) == ("DONE", 2)
    await database.close()


@pytest.mark.asyncio
async def test_consumer_ids_have_independent_deduplication_scope(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    clock = RecordingClock()
    uuid = Uuid7Generator(clock, random_bits=lambda _: 1)
    first = TransactionalInboxConsumer("first", database, clock, uuid)
    second = TransactionalInboxConsumer("second", database, clock, uuid)
    calls: list[str] = []

    async def record(_: SQLiteTransaction, message: Message) -> None:
        calls.append(message.msg_id)

    message = Message("m1", "c1", "business:1")
    assert (await first.consume(message, record)).outcome is ConsumptionOutcome.PROCESSED
    assert (await second.consume(message, record)).outcome is ConsumptionOutcome.PROCESSED
    assert calls == ["m1", "m1"]
    assert len(await database.fetch_all("SELECT * FROM inbox_message")) == 2
    await database.close()


@pytest.mark.asyncio
async def test_persisted_exhausted_retry_is_recovered_to_dead_letter(tmp_path: Path) -> None:
    database, _, consumer = await inbox(
        tmp_path, retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0)
    )
    await consumer._record_retry(Message("m1", "c1"), 2, "old-error")
    calls = 0

    async def must_not_run(_: SQLiteTransaction, __: Message) -> None:
        nonlocal calls
        calls += 1

    result = await consumer.consume(Message("m1", "c1"), must_not_run)

    assert result.outcome is ConsumptionOutcome.DEAD_LETTERED
    assert result.attempts == 2
    assert calls == 0
    assert len(await database.fetch_all("SELECT * FROM dead_letter")) == 1
    await database.close()


def test_retry_policy_validates_configuration_and_delay_input() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(initial_delay_seconds=-1)
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)
    with pytest.raises(ValueError):
        RetryPolicy(max_delay_seconds=-1)
    with pytest.raises(ValueError):
        RetryPolicy().delay_after(0)


@pytest.mark.asyncio
async def test_consumer_validates_identity_and_serialized_size(tmp_path: Path) -> None:
    database, clock, _ = await inbox(tmp_path)
    uuid = Uuid7Generator(clock, random_bits=lambda _: 1)
    with pytest.raises(ValueError, match="consumer_id"):
        TransactionalInboxConsumer("", database, clock, uuid)
    consumer = TransactionalInboxConsumer("size", database, clock, uuid)
    with pytest.raises(ValueError, match="64 KiB"):
        await consumer.consume(Message("m", "c", payload="x" * 65536), write_artifact)
    await database.close()
