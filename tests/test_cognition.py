from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from active_agent_platform import (
    Attention,
    AttentionOutcome,
    FactOutcome,
    ThresholdRule,
    WorldModel,
)
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import Uuid7Generator


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 17, 1, 25, 20, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def event(
    msg_id: str,
    *,
    price: float = 100.0,
    occurred_at: datetime | None = None,
    dedup_key: str | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        msg_id=msg_id,
        msg_type="perception.snapshot",
        source="sensory.test",
        occurred_at=occurred_at or datetime(2026, 8, 17, 1, 25, 10, tzinfo=UTC),
        published_at=datetime(2026, 8, 17, 1, 25, 20, tzinfo=UTC),
        priority=50,
        correlation_id="018f0000-0000-7000-8000-000000000099",
        dedup_key=dedup_key or msg_id,
        payload={
            "event_type": "perception.snapshot",
            "stimulus_id": "TEST",
            "data": {"instrument": "TEST", "price": price},
            "data_quality": "VALID",
            "source_sequence": 1,
        },
    )


UUID_1 = "018f0000-0000-7000-8000-000000000001"
UUID_2 = "018f0000-0000-7000-8000-000000000002"
UUID_3 = "018f0000-0000-7000-8000-000000000003"


def test_world_model_projects_versioned_immutable_snapshot() -> None:
    clock = Clock()
    model = WorldModel(clock)
    update = model.apply(event(UUID_1))
    assert update.outcome is FactOutcome.APPLIED
    assert update.snapshot.version == 1
    record = update.snapshot.get("TEST")
    assert record is not None and record.value["price"] == 100.0
    with pytest.raises(TypeError):
        update.snapshot.facts["other"] = record  # type: ignore[index]
    with pytest.raises(TypeError):
        record.value["price"] = 101  # type: ignore[index]


def test_world_model_handles_duplicate_out_of_order_and_conflict() -> None:
    clock = Clock()
    model = WorldModel(clock)
    first = event(UUID_1)
    assert model.apply(first).outcome is FactOutcome.APPLIED
    assert model.apply(first).outcome is FactOutcome.DUPLICATE
    older = event(UUID_2, occurred_at=first.occurred_at - timedelta(seconds=1))
    assert model.apply(older).outcome is FactOutcome.OUT_OF_ORDER
    conflict = event(UUID_3, price=101, occurred_at=first.occurred_at)
    assert model.apply(conflict).outcome is FactOutcome.CONFLICT
    same = event(UUID_2, occurred_at=first.occurred_at)
    assert model.apply(same).outcome is FactOutcome.DUPLICATE
    assert model.snapshot.version == 1


def test_world_model_expires_stale_facts_from_fresh_snapshot() -> None:
    clock = Clock()
    model = WorldModel(clock, freshness_seconds=10)
    model.apply(event(UUID_1))
    original = model.snapshot
    clock.advance(11)
    fresh = model.fresh_snapshot()
    assert original.get("TEST") is not None
    assert fresh.get("TEST") is None
    assert fresh.version == 2
    assert model.fresh_snapshot() is fresh


@pytest.mark.asyncio
async def test_attention_emits_explainable_salient_event() -> None:
    clock = Clock()
    model = WorldModel(clock)
    baseline_event = event(UUID_1, price=100)
    model.apply(baseline_event)
    emitted: list[EventEnvelope] = []

    async def sink(message: EventEnvelope) -> object:
        emitted.append(message)
        return None

    attention = Attention(
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        (ThresholdRule("price.change.v1", "price", 0.01),),
        sink=sink,
    )
    current = event(UUID_2, price=102, occurred_at=baseline_event.occurred_at + timedelta(seconds=1))
    results = await attention.process(current, model.snapshot)
    result = results[0]
    assert result.outcome is AttentionOutcome.SALIENT
    assert result.score == pytest.approx(0.02)
    assert result.baseline == 100 and result.current == 102
    assert result.evidence_msg_ids == (UUID_2,)
    assert emitted[0].msg_type == "attention.salient_event"
    data = cast(Mapping[str, object], emitted[0].payload["data"])
    assert data["rule_id"] == "price.change.v1"
    assert data["evidence_msg_ids"] == [UUID_2]


@pytest.mark.asyncio
async def test_attention_below_threshold_is_aggregated_without_emission() -> None:
    clock = Clock()
    model = WorldModel(clock)
    model.apply(event(UUID_1, price=100))
    emitted: list[EventEnvelope] = []

    async def sink(message: EventEnvelope) -> object:
        emitted.append(message)
        return None

    attention = Attention(
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        (ThresholdRule("price.change.v1", "price", 0.01),),
        sink=sink,
    )
    for sequence in range(100):
        current = event(
            f"018f0000-0000-7000-8000-{sequence + 100:012d}",
            price=100.5,
            dedup_key=f"observation:{sequence}",
        )
        assert (await attention.process(current, model.snapshot))[0].outcome is AttentionOutcome.BELOW_THRESHOLD
    metrics = attention.metrics()
    assert metrics.evaluated == metrics.below_threshold == 100
    assert metrics.salient == 0
    assert emitted == []


@pytest.mark.asyncio
async def test_attention_dedup_and_cooldown_suppress_repeat_focus() -> None:
    clock = Clock()
    model = WorldModel(clock)
    model.apply(event(UUID_1))
    attention = Attention(
        clock,
        Uuid7Generator(clock, random_bits=lambda _: 1),
        (ThresholdRule("price.change.v1", "price", 0.01, cooldown_seconds=60),),
    )
    first = event(UUID_2, price=102, dedup_key="same")
    duplicate = event(UUID_3, price=102, dedup_key="same")
    cooling = event("018f0000-0000-7000-8000-000000000004", price=103, dedup_key="different")
    assert (await attention.process(first, model.snapshot))[0].outcome is AttentionOutcome.SALIENT
    assert (await attention.process(duplicate, model.snapshot))[0].outcome is AttentionOutcome.DUPLICATE
    assert (await attention.process(cooling, model.snapshot))[0].outcome is AttentionOutcome.COOLDOWN
    clock.advance(61)
    later = event("018f0000-0000-7000-8000-000000000005", price=103, dedup_key="later")
    assert (await attention.process(later, model.snapshot))[0].outcome is AttentionOutcome.SALIENT


def test_cognition_configuration_validation() -> None:
    clock = Clock()
    uuid = Uuid7Generator(clock, random_bits=lambda _: 1)
    with pytest.raises(ValueError):
        WorldModel(clock, freshness_seconds=-1)
    with pytest.raises(ValueError):
        ThresholdRule("", "price", 1)
    with pytest.raises(ValueError):
        ThresholdRule("rule", "price", -1)
    with pytest.raises(ValueError):
        ThresholdRule("rule", "price", 1, cooldown_seconds=-1)
    with pytest.raises(ValueError):
        Attention(clock, uuid, (ThresholdRule("same", "x", 1), ThresholdRule("same", "y", 1)))
    with pytest.raises(ValueError):
        Attention(clock, uuid, (), source="")
