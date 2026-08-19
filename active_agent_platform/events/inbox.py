"""Transactional Inbox consumer with bounded retry and dead-letter handling."""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar, cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator


class InboxMessage(Protocol):
    @property
    def msg_id(self) -> str: ...

    @property
    def correlation_id(self) -> str: ...

    @property
    def dedup_key(self) -> str | None: ...


class InboxStatus(StrEnum):
    RETRY_PENDING = "RETRY_PENDING"
    DONE = "DONE"
    DEAD_LETTER = "DEAD_LETTER"


class ConsumptionOutcome(StrEnum):
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    DEAD_LETTERED = "DEAD_LETTERED"


class RetryableConsumptionError(RuntimeError):
    """Explicitly mark a consumer failure as safe to retry."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.1
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
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
class ConsumptionResult:
    outcome: ConsumptionOutcome
    attempts: int
    error_id: str | None = None


MessageT = TypeVar("MessageT", bound=InboxMessage)
RetryClassifier = Callable[[Exception], bool]
MessageSerializer = Callable[[InboxMessage], str]


class TransactionalInboxConsumer:
    """Run one message's database effects and Inbox completion atomically."""

    def __init__(
        self,
        consumer_id: str,
        database: SQLiteDatabase,
        clock: Clock,
        uuid: UuidGenerator,
        *,
        retry_policy: RetryPolicy | None = None,
        is_retryable: RetryClassifier | None = None,
        serializer: MessageSerializer | None = None,
    ) -> None:
        if not consumer_id:
            raise ValueError("consumer_id must not be empty")
        self._consumer_id = consumer_id
        self._database = database
        self._clock = clock
        self._uuid = uuid
        self._retry_policy = retry_policy or RetryPolicy()
        self._is_retryable = is_retryable or (
            lambda error: isinstance(error, RetryableConsumptionError)
        )
        self._serializer = serializer or _serialize_message

    async def consume(
        self,
        message: MessageT,
        handler: Callable[[SQLiteTransaction, MessageT], Awaitable[None]],
    ) -> ConsumptionResult:
        envelope_json = self._serializer(message)
        if len(envelope_json.encode("utf-8")) > 65536:
            raise ValueError("serialized message must not exceed 64 KiB")

        status, previous_failures = await self._state(message)
        if status in {InboxStatus.DONE, InboxStatus.DEAD_LETTER}:
            return ConsumptionResult(ConsumptionOutcome.DUPLICATE, previous_failures)
        for attempt in range(previous_failures + 1, self._retry_policy.max_attempts + 1):
            try:
                duplicate = await self._run_attempt(message, handler, attempt)
            except Exception as error:  # noqa: BLE001 - handlers report failure by raising
                retryable = self._is_retryable(error)
                exhausted = attempt >= self._retry_policy.max_attempts
                error_id = str(self._uuid.new())
                if not retryable or exhausted:
                    await self._record_dead_letter(
                        message, envelope_json, attempt, error_id
                    )
                    return ConsumptionResult(
                        ConsumptionOutcome.DEAD_LETTERED, attempt, error_id
                    )
                await self._record_retry(message, attempt, error_id)
                await self._clock.sleep(self._retry_policy.delay_after(attempt))
                continue
            if duplicate:
                return ConsumptionResult(ConsumptionOutcome.DUPLICATE, previous_failures)
            return ConsumptionResult(ConsumptionOutcome.PROCESSED, attempt)

        # A persisted exhausted record can only be reached after an interrupted old version.
        error_id = str(self._uuid.new())
        await self._record_dead_letter(
            message, envelope_json, previous_failures, error_id
        )
        return ConsumptionResult(
            ConsumptionOutcome.DEAD_LETTERED, previous_failures, error_id
        )

    async def _state(self, message: InboxMessage) -> tuple[str | None, int]:
        row = await self._database.fetch_one(
            """
            SELECT status, attempt FROM inbox_message
            WHERE consumer_id = ? AND (msg_id = ? OR (dedup_key IS NOT NULL AND dedup_key = ?))
            LIMIT 1
            """,
            (self._consumer_id, message.msg_id, message.dedup_key),
        )
        if row is None:
            return None, 0
        return str(row["status"]), int(row["attempt"])

    async def _run_attempt(
        self,
        message: MessageT,
        handler: Callable[[SQLiteTransaction, MessageT], Awaitable[None]],
        attempt: int,
    ) -> bool:
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                """
                SELECT status FROM inbox_message
                WHERE consumer_id = ?
                  AND (msg_id = ? OR (dedup_key IS NOT NULL AND dedup_key = ?))
                LIMIT 1
                """,
                (self._consumer_id, message.msg_id, message.dedup_key),
            )
            if existing is not None and str(existing["status"]) in {
                InboxStatus.DONE,
                InboxStatus.DEAD_LETTER,
            }:
                return True
            if existing is None:
                await transaction.execute(
                    """
                    INSERT INTO inbox_message(
                        consumer_id, msg_id, dedup_key, status, attempt,
                        received_at, correlation_id
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        self._consumer_id,
                        message.msg_id,
                        message.dedup_key,
                        InboxStatus.RETRY_PENDING,
                        _timestamp(self._clock.now()),
                        message.correlation_id,
                    ),
                )
            await handler(transaction, message)
            await transaction.execute(
                """
                UPDATE inbox_message
                SET status = ?, attempt = ?, processed_at = ?, error_id = NULL
                WHERE consumer_id = ? AND msg_id = ?
                """,
                (
                    InboxStatus.DONE,
                    attempt,
                    _timestamp(self._clock.now()),
                    self._consumer_id,
                    message.msg_id,
                ),
            )
            return False

    async def _record_retry(
        self, message: InboxMessage, attempt: int, error_id: str
    ) -> None:
        async with self._database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO inbox_message(
                    consumer_id, msg_id, dedup_key, status, attempt,
                    received_at, error_id, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer_id, msg_id) DO UPDATE SET
                    status = excluded.status,
                    attempt = excluded.attempt,
                    error_id = excluded.error_id
                """,
                (
                    self._consumer_id,
                    message.msg_id,
                    message.dedup_key,
                    InboxStatus.RETRY_PENDING,
                    attempt,
                    _timestamp(self._clock.now()),
                    error_id,
                    message.correlation_id,
                ),
            )

    async def _record_dead_letter(
        self,
        message: InboxMessage,
        envelope_json: str,
        attempt: int,
        error_id: str,
    ) -> None:
        failed_at = _timestamp(self._clock.now())
        async with self._database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO inbox_message(
                    consumer_id, msg_id, dedup_key, status, attempt,
                    received_at, processed_at, error_id, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer_id, msg_id) DO UPDATE SET
                    status = excluded.status,
                    attempt = excluded.attempt,
                    processed_at = excluded.processed_at,
                    error_id = excluded.error_id
                """,
                (
                    self._consumer_id,
                    message.msg_id,
                    message.dedup_key,
                    InboxStatus.DEAD_LETTER,
                    attempt,
                    failed_at,
                    failed_at,
                    error_id,
                    message.correlation_id,
                ),
            )
            await transaction.execute(
                """
                INSERT INTO dead_letter(
                    dead_letter_id, consumer_id, msg_id, envelope_json,
                    error_id, failed_at, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(self._uuid.new()),
                    self._consumer_id,
                    message.msg_id,
                    envelope_json,
                    error_id,
                    failed_at,
                    message.correlation_id,
                ),
            )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _serialize_message(message: InboxMessage) -> str:
    value: object
    if is_dataclass(message):
        value = asdict(cast(Any, message))
    elif isinstance(message, Mapping):
        value = dict(message)
    elif hasattr(message, "__dict__"):
        value = vars(message)
    else:
        raise TypeError("message requires a serializer")
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
