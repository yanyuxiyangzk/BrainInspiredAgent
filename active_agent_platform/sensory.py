"""JSONL sensory adapter and governed external command adapter."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from active_agent_platform.events import EventEnvelope
from brain_kernel.ports import Clock, UuidGenerator


class InputOutcome(StrEnum):
    PUBLISHED = "PUBLISHED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class DataQuality(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class InputResult:
    outcome: InputOutcome
    msg_id: str | None = None
    error_code: str | None = None
    source_sequence: int | None = None


class EventSink(Protocol):
    async def publish(self, message: EventEnvelope) -> object: ...


class InputRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class JsonlSensory:
    name = "jsonl_sensory"

    def __init__(
        self,
        source: str,
        clock: Clock,
        uuid: UuidGenerator,
        sink: EventSink,
        *,
        freshness_seconds: float = 15.0,
        future_tolerance_seconds: float = 0.0,
        source_id: str | None = None,
    ) -> None:
        if not source or not source_id and not source:
            raise ValueError("source must not be empty")
        if freshness_seconds < 0:
            raise ValueError("freshness_seconds must be non-negative")
        if future_tolerance_seconds < 0:
            raise ValueError("future_tolerance_seconds must be non-negative")
        self._source = source
        self._source_id = source_id or source
        self._clock = clock
        self._uuid = uuid
        self._sink = sink
        self._freshness = freshness_seconds
        self._future_tolerance = future_tolerance_seconds
        self._last_sequence: int | None = None
        self._seen_keys: set[str] = set()
        self._accepting = False

    async def start(self) -> None:
        self._accepting = True

    async def serve(self) -> None:
        return None

    async def quiesce(self) -> None:
        self._accepting = False

    async def checkpoint(self) -> None:
        return None

    async def stop(self) -> None:
        self._accepting = False

    async def ingest_file(self, path: str | Path) -> tuple[InputResult, ...]:
        results: list[InputResult] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            results.append(await self.ingest_line(line))
        return tuple(results)

    async def ingest_lines(self, lines: Iterable[str]) -> tuple[InputResult, ...]:
        return tuple([await self.ingest_line(line) for line in lines])

    async def ingest_line(self, line: str) -> InputResult:
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise InputRejected("SENSORY_JSON_INVALID", "JSONL row must be an object")
            event_time = _parse_time(value.get("event_time"))
            sequence = _sequence(value)
            if self._last_sequence is not None and sequence < self._last_sequence:
                raise InputRejected("SENSORY_SEQUENCE_OUT_OF_ORDER", "source sequence moved backwards")
            now = self._clock.now()
            age = (now - event_time).total_seconds()
            if age < -self._future_tolerance:
                raise InputRejected("SENSORY_EVENT_IN_FUTURE", "event time exceeds clock tolerance")
            key = f"{self._source_id}:{sequence}"
            if sequence == self._last_sequence or key in self._seen_keys:
                return InputResult(InputOutcome.DUPLICATE, source_sequence=sequence)
            quality = DataQuality(str(value.get("data_quality", "VALID")))
            if age > self._freshness and quality is DataQuality.VALID:
                quality = DataQuality.STALE
            data = value.get("data", {key: item for key, item in value.items() if key not in {
                "event_time", "source_seq", "source_sequence", "data_quality"
            }})
            if not isinstance(data, Mapping):
                raise InputRejected("SENSORY_DATA_INVALID", "data must be an object")
            msg_id = str(self._uuid.new())
            envelope = EventEnvelope(
                msg_id=msg_id,
                msg_type="perception.snapshot",
                source=self._source,
                occurred_at=event_time,
                published_at=now,
                priority=int(value.get("priority", 50)),
                correlation_id=msg_id,
                dedup_key=key,
                payload={
                    "event_type": "perception.snapshot",
                    "stimulus_id": key,
                    "data": dict(data),
                    "data_quality": quality.value,
                    "source_sequence": sequence,
                },
            )
            await self._sink.publish(envelope)
            self._last_sequence = sequence
            self._seen_keys.add(key)
            return InputResult(InputOutcome.PUBLISHED, msg_id, source_sequence=sequence)
        except InputRejected as error:
            return InputResult(InputOutcome.REJECTED, error_code=error.code)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return InputResult(InputOutcome.REJECTED, error_code="SENSORY_JSON_INVALID")


class CommandRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CommandAdapter:
    name = "command_adapter"

    def __init__(
        self,
        source: str,
        clock: Clock,
        uuid: UuidGenerator,
        sink: EventSink,
        *,
        allowed_commands: Mapping[str, bool] | None = None,
    ) -> None:
        if not source:
            raise ValueError("source must not be empty")
        self._source = source
        self._clock = clock
        self._uuid = uuid
        self._sink = sink
        self._allowed = dict(allowed_commands or {"status": False, "refresh": False})

    async def inject(
        self,
        command: str,
        args: Mapping[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
        priority: int = 70,
    ) -> InputResult:
        if command not in self._allowed:
            raise CommandRejected("COMMAND_NOT_ALLOWED", "command is not on the allowlist")
        if self._allowed[command]:
            raise CommandRejected("COMMAND_REQUIRES_GOVERNANCE", "side-effect command must enter planning gates")
        if not 0 <= priority <= 100:
            raise CommandRejected("COMMAND_PRIORITY_INVALID", "priority must be between 0 and 100")
        msg_id = str(self._uuid.new())
        dedup = idempotency_key or f"command:{command}:{msg_id}"
        event = EventEnvelope(
            msg_id=msg_id,
            msg_type="command.received",
            source=self._source,
            occurred_at=self._clock.now(),
            published_at=self._clock.now(),
            priority=priority,
            correlation_id=msg_id,
            dedup_key=dedup,
            payload={
                "event_type": "command.received",
                "stimulus_id": dedup,
                "data": {"command": command, "args": dict(args or {}), "governed": True},
                "data_quality": "VALID",
            },
        )
        await self._sink.publish(event)
        return InputResult(InputOutcome.PUBLISHED, msg_id)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise InputRejected("SENSORY_EVENT_TIME_INVALID", "event_time must be ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InputRejected("SENSORY_EVENT_TIME_INVALID", "event_time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputRejected("SENSORY_EVENT_TIME_INVALID", "event_time must include timezone")
    return parsed.astimezone(UTC)


def _sequence(value: Mapping[str, object]) -> int:
    raw = value.get("source_sequence", value.get("source_seq"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise InputRejected("SENSORY_SEQUENCE_INVALID", "source sequence must be a non-negative integer")
    return raw
