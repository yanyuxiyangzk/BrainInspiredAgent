import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.events import (
    EventBus,
    EventEnvelope,
    EventRegistration,
    EventSchemaRegistry,
    EventValidationError,
    OutboxWriter,
    SubscriptionConfig,
)
from active_agent_platform.storage import SQLiteDatabase

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 1, 25, 20, tzinfo=UTC)
UUID_1 = "018f0000-0000-7000-8000-000000000001"
UUID_2 = "018f0000-0000-7000-8000-000000000002"


def registry() -> EventSchemaRegistry:
    return EventSchemaRegistry.from_schema_files(ROOT / "schemas" / "event")


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


def test_valid_envelope_round_trips_and_is_immutable() -> None:
    event = valid_event(trace_context={"trace_id": "t1"})
    document = event.to_dict()
    assert document["schema_version"] == "1.0"
    assert document["occurred_at"] == "2026-08-17T01:25:20Z"
    assert EventEnvelope.from_dict(document) == event
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]


def test_registry_validates_core_schema_and_event_type_consistency() -> None:
    event = valid_event()
    assert registry().validate(event) == event
    with pytest.raises(EventValidationError, match="event_type"):
        registry().validate(valid_event(payload={"event_type": "command.received"}))
    with pytest.raises(EventValidationError, match="data_quality"):
        registry().validate(
            valid_event(
                payload={
                    "event_type": "perception.snapshot",
                    "stimulus_id": "market:1",
                    "data": {},
                    "data_quality": "BROKEN",
                }
            )
        )


def test_registry_rejects_unknown_type_and_duplicate_registration() -> None:
    event = valid_event(msg_type="custom.event", payload={"event_type": "custom.event"})
    with pytest.raises(EventValidationError, match="unregistered"):
        registry().validate(event)
    custom = EventSchemaRegistry({"type": "object"})
    registration = EventRegistration("custom.event", {"type": "object"})
    custom.register(registration)
    with pytest.raises(ValueError, match="duplicate"):
        custom.register(registration)
    assert custom.contains("custom.event")


def test_registry_checks_priority_and_envelope_fields() -> None:
    custom = EventSchemaRegistry(
        {"type": "object", "required": ["priority"]},
    )
    custom.register(EventRegistration("custom.event", {"type": "object"}, min_priority=90))
    event = valid_event(msg_type="custom.event", payload={"event_type": "custom.event"})
    with pytest.raises(EventValidationError, match="priority"):
        custom.validate(event)
    with pytest.raises(ValueError, match="UUID"):
        valid_event(msg_id="bad-id")


@pytest.mark.asyncio
async def test_event_bus_rejects_invalid_event_before_enqueue(tmp_path: Path) -> None:
    bus = EventBus(schema_registry=registry())
    subscription = bus.subscribe(SubscriptionConfig("target", frozenset({"perception.snapshot"})))
    await bus.start()
    with pytest.raises(EventValidationError):
        await bus.publish(valid_event(payload={"event_type": "command.received"}))
    assert subscription.qsize() == 0
    await bus.stop()


class Clock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.mark.asyncio
async def test_outbox_writer_rejects_invalid_event_before_insert(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    writer = OutboxWriter(Clock(), schema_registry=registry())
    with pytest.raises(EventValidationError):
        async with database.transaction() as transaction:
            await writer.append(transaction, valid_event(payload={"event_type": "command.received"}))
    assert await database.fetch_one("SELECT * FROM outbox_event") is None
    await database.close()


def test_schema_documents_are_machine_readable() -> None:
    envelope = json.loads((ROOT / "schemas/event/event-envelope-1.0.schema.json").read_text())
    payload = json.loads((ROOT / "schemas/event/core-event-payload-1.0.schema.json").read_text())
    assert envelope["properties"]["schema_version"]["const"] == "1.0"
    assert len(payload["oneOf"]) == 3
