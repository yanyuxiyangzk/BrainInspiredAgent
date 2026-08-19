"""Event Envelope/Payload 1.0 models and registry-backed validation."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]


class EventValidationError(ValueError):
    def __init__(self, message: str, *, path: tuple[object, ...] = ()) -> None:
        self.path = path
        super().__init__(message)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    msg_id: str
    msg_type: str
    source: str
    occurred_at: datetime
    published_at: datetime
    priority: int
    correlation_id: str
    dedup_key: str
    payload: Mapping[str, object]
    target: str | None = None
    causation_id: str | None = None
    expires_at: datetime | None = None
    trace_context: Mapping[str, object] = field(default_factory=dict)
    payload_schema: str = "schema://event/core-event-payload/1.0"
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        for name, value in (("msg_id", self.msg_id), ("correlation_id", self.correlation_id)):
            try:
                UUID(value)
            except (ValueError, AttributeError) as error:
                raise ValueError(f"{name} must be a UUID string") from error
        if self.causation_id is not None:
            try:
                UUID(self.causation_id)
            except (ValueError, AttributeError) as error:
                raise ValueError("causation_id must be a UUID string") from error
        if not self.msg_type or "." not in self.msg_type:
            raise ValueError("msg_type must be a dotted event type")
        if not self.source or len(self.source) > 100:
            raise ValueError("source must be between 1 and 100 characters")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        if not self.dedup_key or len(self.dedup_key) > 255:
            raise ValueError("dedup_key must be between 1 and 255 characters")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        object.__setattr__(self, "published_at", _utc(self.published_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "trace_context", MappingProxyType(dict(self.trace_context)))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "source": self.source,
            "occurred_at": _iso(self.occurred_at),
            "published_at": _iso(self.published_at),
            "priority": self.priority,
            "correlation_id": self.correlation_id,
            "dedup_key": self.dedup_key,
            "payload": _plain(self.payload),
            "payload_schema": self.payload_schema,
        }
        if self.target is not None:
            result["target"] = self.target
        if self.causation_id is not None:
            result["causation_id"] = self.causation_id
        if self.expires_at is not None:
            result["expires_at"] = _iso(self.expires_at)
        if self.trace_context:
            result["trace_context"] = _plain(self.trace_context)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EventEnvelope":
        try:
            return cls(
                msg_id=str(value["msg_id"]),
                msg_type=str(value["msg_type"]),
                source=str(value["source"]),
                occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
                published_at=datetime.fromisoformat(str(value["published_at"])),
                priority=int(cast(int | str, value["priority"])),
                correlation_id=str(value["correlation_id"]),
                dedup_key=str(value["dedup_key"]),
                payload=value["payload"] if isinstance(value["payload"], Mapping) else {},
                target=str(value["target"]) if value.get("target") is not None else None,
                causation_id=str(value["causation_id"]) if value.get("causation_id") is not None else None,
                expires_at=(
                    datetime.fromisoformat(str(value["expires_at"]))
                    if value.get("expires_at") is not None
                    else None
                ),
                trace_context=(
                    cast(Mapping[str, object], value["trace_context"])
                    if isinstance(value.get("trace_context"), Mapping)
                    else {}
                ),
                payload_schema=str(value.get("payload_schema", "schema://event/core-event-payload/1.0")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise EventValidationError("invalid Event Envelope 1.0 fields") from error


@dataclass(frozen=True, slots=True)
class EventRegistration:
    msg_type: str
    payload_schema: Mapping[str, object]
    publisher: str = "*"
    consumers: tuple[str, ...] = ()
    min_priority: int = 0
    max_priority: int = 100


class EventSchemaRegistry:
    def __init__(
        self,
        envelope_schema: Mapping[str, object],
        *,
        payload_schema: Mapping[str, object] | None = None,
    ) -> None:
        self._envelope = Draft202012Validator(envelope_schema, format_checker=FormatChecker())
        self._default_payload = (
            Draft202012Validator(payload_schema, format_checker=FormatChecker())
            if payload_schema is not None
            else None
        )
        self._core_types = frozenset(
            {
                "world.snapshot_created", "plan.candidate_created", "plan.decided",
                "execution.granted", "task.started", "task.finished", "task.failed",
                "outcome.evaluated", "memory.consolidated", "evolution.proposed",
                "perception.snapshot", "command.received", "schedule.triggered",
                "attention.salient_event", "brain.state_changed", "goal.changed",
                "system.health_changed",
            }
        ) if payload_schema is not None else frozenset()
        self._registrations: dict[str, tuple[EventRegistration, Draft202012Validator]] = {}

    @classmethod
    def from_schema_files(cls, root: str | Path) -> "EventSchemaRegistry":
        path = Path(root)
        envelope = json.loads((path / "event-envelope-1.0.schema.json").read_text())
        payload = json.loads((path / "core-event-payload-1.0.schema.json").read_text())
        return cls(envelope, payload_schema=payload)

    def register(self, registration: EventRegistration) -> None:
        if registration.msg_type in self._registrations:
            raise ValueError(f"duplicate event registration: {registration.msg_type}")
        if not 0 <= registration.min_priority <= registration.max_priority <= 100:
            raise ValueError("registration priority range is invalid")
        validator = Draft202012Validator(registration.payload_schema, format_checker=FormatChecker())
        self._registrations[registration.msg_type] = (registration, validator)

    def validate(self, event: EventEnvelope | Mapping[str, object]) -> EventEnvelope:
        envelope = event if isinstance(event, EventEnvelope) else EventEnvelope.from_dict(event)
        document = envelope.to_dict()
        errors = sorted(self._envelope.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            raise EventValidationError(error.message, path=tuple(error.path))
        registration = self._registrations.get(envelope.msg_type)
        validator = registration[1] if registration is not None else self._default_payload
        if registration is None and not self._registrations and validator is None:
            raise EventValidationError("event registry has no payload schema")
        if registration is None and self._registrations:
            raise EventValidationError(f"unregistered event type: {envelope.msg_type}")
        if registration is None and envelope.msg_type not in self._core_types:
            raise EventValidationError(f"unregistered event type: {envelope.msg_type}")
        if registration is not None:
            metadata = registration[0]
            if not metadata.min_priority <= envelope.priority <= metadata.max_priority:
                raise EventValidationError("event priority is outside registration range")
        if validator is not None:
            payload_errors = sorted(validator.iter_errors(document["payload"]), key=lambda error: list(error.path))
            if payload_errors:
                error = payload_errors[0]
                raise EventValidationError(error.message, path=("payload", *error.path))
        if envelope.payload.get("event_type") != envelope.msg_type:
            raise EventValidationError("payload.event_type must equal envelope.msg_type", path=("payload", "event_type"))
        return envelope

    def contains(self, msg_type: str) -> bool:
        return msg_type in self._registrations
