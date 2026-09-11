"""Branch-completion tests for Event Envelope and schema registry validation."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.events import (
    EventEnvelope,
    EventRegistration,
    EventSchemaRegistry,
    EventValidationError,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 1, 25, 20, tzinfo=UTC)
UUID_1 = "018f0000-0000-7000-8000-000000000001"
UUID_2 = "018f0000-0000-7000-8000-000000000002"


def valid_event(**changes: object) -> EventEnvelope:
    values: dict[str, object] = {
        "msg_id": UUID_1,
        "msg_type": "perception.snapshot",
        "source": "sensory.market",
        "occurred_at": NOW,
        "published_at": NOW,
        "priority": 50,
        "correlation_id": UUID_2,
        "dedup_key": "market:20260817:092520",
        "payload": {
            "event_type": "perception.snapshot",
            "stimulus_id": "market:1",
            "data": {"price": 1},
            "data_quality": "VALID",
            "source_sequence": 1,
        },
    }
    values.update(changes)
    return EventEnvelope(**values)  # type: ignore[arg-type]


def test_rejects_naive_timestamps_and_optional_uuid_fields() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        valid_event(occurred_at=datetime(2026, 8, 17, 1, 25, 20))  # noqa: DTZ001 - 测试朴素时间戳被拒绝
    with pytest.raises(ValueError, match="correlation_id"):
        valid_event(correlation_id="not-a-uuid")
    with pytest.raises(ValueError, match="causation_id"):
        valid_event(causation_id="nope")
    with pytest.raises(ValueError, match="dotted"):
        valid_event(msg_type="nodot")
    with pytest.raises(ValueError, match="source"):
        valid_event(source="")
    with pytest.raises(ValueError, match="source"):
        valid_event(source="x" * 101)
    with pytest.raises(ValueError, match="priority"):
        valid_event(priority=101)
    with pytest.raises(ValueError, match="priority"):
        valid_event(priority=-1)
    with pytest.raises(ValueError, match="dedup_key"):
        valid_event(dedup_key="")
    with pytest.raises(ValueError, match="dedup_key"):
        valid_event(dedup_key="x" * 256)


def test_optional_fields_convert_to_utc_and_round_trip() -> None:
    event = valid_event(
        target="consumer.a",
        causation_id=UUID_1,
        expires_at=datetime(2026, 8, 17, 2, 0, 0, tzinfo=UTC),
        trace_context={"trace_id": "t1"},
    )
    document = event.to_dict()
    assert document["target"] == "consumer.a"
    assert document["causation_id"] == UUID_1
    assert document["expires_at"] == "2026-08-17T02:00:00Z"
    assert document["trace_context"] == {"trace_id": "t1"}
    assert EventEnvelope.from_dict(document) == event


def test_from_dict_wraps_malformed_documents() -> None:
    with pytest.raises(EventValidationError, match="invalid Event Envelope"):
        EventEnvelope.from_dict({"msg_id": UUID_1})  # type: ignore[arg-type]
    with pytest.raises(EventValidationError, match="invalid Event Envelope"):
        EventEnvelope.from_dict({**valid_event().to_dict(), "occurred_at": "not-a-date"})


def test_rejects_non_mapping_payload_silently_normalized() -> None:
    document = valid_event().to_dict()
    document["payload"] = "not-a-mapping"  # type: ignore[assignment]
    event = EventEnvelope.from_dict(document)
    assert dict(event.payload) == {}


def make_registry() -> EventSchemaRegistry:
    return EventSchemaRegistry.from_schema_files(ROOT / "schemas" / "event")


def test_registration_rejects_duplicates_and_bad_priority_range() -> None:
    registry = make_registry()
    schema = {"type": "object"}
    registry.register(EventRegistration(msg_type="custom.thing", payload_schema=schema))
    with pytest.raises(ValueError, match="duplicate event registration"):
        registry.register(EventRegistration(msg_type="custom.thing", payload_schema=schema))
    with pytest.raises(ValueError, match="priority range"):
        registry.register(
            EventRegistration(msg_type="custom.other", payload_schema=schema, max_priority=200)
        )


def test_registry_validates_envelope_before_payload() -> None:
    strict = EventSchemaRegistry(
        {
            "type": "object",
            "required": ["msg_id", "msg_type", "priority"],
            "properties": {"priority": {"maximum": 10}},
        }
    )
    with pytest.raises(EventValidationError):
        strict.validate(valid_event())


def test_registry_requires_default_or_core_type_when_no_registrations() -> None:
    bare = EventSchemaRegistry({"type": "object"})
    with pytest.raises(EventValidationError, match="no payload schema"):
        bare.validate(valid_event())


def test_registry_rejects_unregistered_custom_types() -> None:
    registry = make_registry()
    with pytest.raises(EventValidationError, match="unregistered event type"):
        registry.validate(valid_event(msg_type="custom.unknown"))


def test_registration_scopes_priority_range_and_payload() -> None:
    registry = make_registry()
    schema = {
        "type": "object",
        "required": ["event_type"],
        "properties": {"event_type": {"type": "string"}},
    }
    registry.register(
        EventRegistration(msg_type="custom.thing", payload_schema=schema, max_priority=10)
    )
    event = valid_event(
        msg_type="custom.thing",
        priority=5,
        payload={"event_type": "custom.thing"},
    )
    assert registry.validate(event) == event

    with pytest.raises(EventValidationError, match="priority"):
        registry.validate(
            valid_event(msg_type="custom.thing", priority=50, payload={"event_type": "custom.thing"})
        )
    with pytest.raises(EventValidationError, match="event_type"):  # payload 缺少必填字段
        registry.validate(valid_event(msg_type="custom.thing", priority=5, payload={}))
    with pytest.raises(EventValidationError, match="event_type"):  # payload 通过 Schema 但与信封不一致
        registry.validate(
            valid_event(msg_type="custom.thing", priority=5, payload={"event_type": "custom.other"})
        )
