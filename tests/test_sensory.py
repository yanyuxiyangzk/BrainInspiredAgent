from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform import (
    CommandAdapter,
    CommandRejected,
    DataQuality,
    InputOutcome,
    JsonlSensory,
)
from active_agent_platform.foundation import Uuid7Generator


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 17, 1, 25, 20, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


class Sink:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def publish(self, message: object) -> None:
        self.messages.append(message)


def row(sequence: int, event_time: str = "2026-08-17T01:25:10Z") -> str:
    return (
        f'{{"event_time":"{event_time}","source_seq":{sequence},"instrument":"TEST","price":100}}'
    )


async def setup() -> tuple[Clock, Sink, JsonlSensory]:
    clock = Clock()
    sink = Sink()
    sensory = JsonlSensory(
        "sensory.test",
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        sink,
        freshness_seconds=15,
    )
    return clock, sink, sensory


@pytest.mark.asyncio
async def test_jsonl_publishes_normalized_event_and_preserves_sequence() -> None:
    _, sink, sensory = await setup()
    result = await sensory.ingest_line(row(1))
    event = sink.messages[0]
    assert result.outcome is InputOutcome.PUBLISHED
    assert result.source_sequence == 1
    assert event.msg_type == "perception.snapshot"
    assert event.payload["event_type"] == "perception.snapshot"
    assert event.payload["source_sequence"] == 1
    assert event.payload["data_quality"] == DataQuality.VALID
    assert event.payload["data"]["instrument"] == "TEST"


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_sequences_are_identifiable() -> None:
    _, sink, sensory = await setup()
    first = await sensory.ingest_line(row(2))
    duplicate = await sensory.ingest_line(row(2))
    out_of_order = await sensory.ingest_line(row(1))
    assert first.outcome is InputOutcome.PUBLISHED
    assert duplicate.outcome is InputOutcome.DUPLICATE
    assert out_of_order.outcome is InputOutcome.REJECTED
    assert out_of_order.error_code == "SENSORY_SEQUENCE_OUT_OF_ORDER"
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_stale_and_future_data_quality_and_stable_rejection() -> None:
    clock, _, sensory = await setup()
    stale = await sensory.ingest_line(row(1, "2026-08-17T01:24:00Z"))
    future = await sensory.ingest_line(row(2, "2026-08-17T01:25:21Z"))
    assert stale.outcome is InputOutcome.PUBLISHED
    assert future.outcome is InputOutcome.REJECTED
    assert future.error_code == "SENSORY_EVENT_IN_FUTURE"
    clock.value += timedelta(seconds=1)


@pytest.mark.asyncio
async def test_jsonl_rejects_malformed_rows_and_invalid_sequences() -> None:
    _, sink, sensory = await setup()
    results = await sensory.ingest_lines(
        [
            "not-json",
            "[]",
            '{"event_time":"2026-08-17T01:25:10Z","source_seq":true}',
            '{"event_time":"2026-08-17T01:25:10Z","source_seq":1,"data":[]}',
        ]
    )
    assert all(item.outcome is InputOutcome.REJECTED for item in results)
    assert len(sink.messages) == 0


@pytest.mark.asyncio
async def test_sensory_lifecycle_and_file_input(tmp_path: Path) -> None:
    _, sink, sensory = await setup()
    path = tmp_path / "observations.jsonl"
    path.write_text(row(1) + "\n" + row(2), encoding="utf-8")
    await sensory.start()
    results = await sensory.ingest_file(path)
    await sensory.checkpoint()
    await sensory.quiesce()
    await sensory.stop()
    assert [item.outcome for item in results] == [InputOutcome.PUBLISHED, InputOutcome.PUBLISHED]
    assert len(sink.messages) == 2


@pytest.mark.asyncio
async def test_command_adapter_only_emits_governed_command_event() -> None:
    clock = Clock()
    sink = Sink()
    adapter = CommandAdapter(
        "cli.test",
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 2),
        sink,
        allowed_commands={"status": False, "summarize": True},
    )
    result = await adapter.inject("status", {"format": "json"}, idempotency_key="status:1")
    event = sink.messages[0]
    assert result.outcome is InputOutcome.PUBLISHED
    assert event.msg_type == "command.received"
    assert event.payload["data"]["governed"] is True
    assert event.payload["data"]["command"] == "status"
    with pytest.raises(CommandRejected, match="allowlist"):
        await adapter.inject("unknown")
    with pytest.raises(CommandRejected, match="planning"):
        await adapter.inject("summarize")
    with pytest.raises(CommandRejected, match="priority"):
        await adapter.inject("status", priority=101)


def test_adapter_and_sensory_configuration_validation() -> None:
    clock = Clock()
    sink = Sink()
    uuid = Uuid7Generator(clock, random_bits=lambda _: 1)
    with pytest.raises(ValueError):
        JsonlSensory("", clock, uuid, sink)
    with pytest.raises(ValueError):
        JsonlSensory("source", clock, uuid, sink, freshness_seconds=-1)
    with pytest.raises(ValueError):
        JsonlSensory("source", clock, uuid, sink, future_tolerance_seconds=-1)
    with pytest.raises(ValueError):
        CommandAdapter("", clock, uuid, sink)
