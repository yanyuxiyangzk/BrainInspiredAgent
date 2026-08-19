import asyncio
import time
from dataclasses import dataclass

import pytest

from active_agent_platform.events import (
    DeliveryOutcome,
    EventBus,
    EventQueueFullError,
    OverflowPolicy,
    SubscriptionClosedError,
    SubscriptionConfig,
)


@dataclass(frozen=True, slots=True)
class FakeMessage:
    msg_id: str
    msg_type: str = "test.event"
    source: str = "source-a"
    priority: int = 50
    source_sequence: int = 0
    group: str = "default"
    created_at: float = 0.0


async def started_bus() -> EventBus:
    bus = EventBus()
    await bus.start()
    return bus


def config(
    subscription_id: str,
    *,
    capacity: int = 100,
    policy: OverflowPolicy = OverflowPolicy.REJECT,
    message_types: frozenset[str] = frozenset({"test.event"}),
    **overrides: object,
) -> SubscriptionConfig:
    return SubscriptionConfig(
        subscription_id,
        message_types,
        capacity,
        policy,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_publish_fans_out_same_immutable_message_to_independent_subscribers() -> None:
    bus = await started_bus()
    first = bus.subscribe(config("first"))
    second = bus.subscribe(config("second"))
    message = FakeMessage("message-1")

    report = await bus.publish(message)
    first_received, second_received = await asyncio.gather(first.get(), second.get())

    assert first_received is message
    assert second_received is message
    assert first_received.msg_id == second_received.msg_id
    assert [result.outcome for result in report.deliveries] == [
        DeliveryOutcome.ENQUEUED,
        DeliveryOutcome.ENQUEUED,
    ]
    await bus.stop()


@pytest.mark.asyncio
async def test_message_type_and_predicate_filters_are_subscription_local() -> None:
    bus = await started_bus()
    selected = bus.subscribe(
        config("selected", predicate=lambda message: message.source == "source-a")
    )
    other_type = bus.subscribe(
        config("other", message_types=frozenset({"other.event"}))
    )

    report = await bus.publish(FakeMessage("message-1"))

    assert selected.qsize() == 1
    assert other_type.qsize() == 0
    assert {result.subscription_id: result.outcome for result in report.deliveries} == {
        "other": DeliveryOutcome.FILTERED,
        "selected": DeliveryOutcome.ENQUEUED,
    }
    assert other_type.metrics().filtered == 1
    await bus.stop()


@pytest.mark.asyncio
async def test_predicate_false_filters_without_affecting_matching_subscription() -> None:
    bus = await started_bus()
    filtered = bus.subscribe(
        config("filtered", predicate=lambda message: message.source == "other-source")
    )
    matching = bus.subscribe(config("matching"))
    report = await bus.publish(FakeMessage("message-1"))
    assert filtered.qsize() == 0
    assert matching.qsize() == 1
    assert filtered.metrics().filtered == 1
    assert {item.subscription_id: item.outcome for item in report.deliveries}[
        "filtered"
    ] is DeliveryOutcome.FILTERED
    await bus.stop()


@pytest.mark.asyncio
async def test_predicate_failure_does_not_block_other_subscription() -> None:
    bus = await started_bus()

    def broken_predicate(message: object) -> bool:
        raise RuntimeError(f"cannot inspect {type(message).__name__}")

    broken = bus.subscribe(config("broken", predicate=broken_predicate))
    healthy = bus.subscribe(config("healthy"))
    report = await bus.publish(FakeMessage("message-1"))

    assert healthy.qsize() == 1
    assert broken.qsize() == 0
    assert report.rejected == ("broken",)
    assert broken.metrics().rejected == 1
    await bus.stop()


@pytest.mark.asyncio
async def test_priority_across_sources_never_reorders_same_source() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(config("ordered", capacity=10))
    messages = (
        FakeMessage("a-1", source="source-a", priority=1, source_sequence=1),
        FakeMessage("a-2", source="source-a", priority=100, source_sequence=2),
        FakeMessage("b-1", source="source-b", priority=50, source_sequence=1),
    )
    for message in messages:
        await bus.publish(message)

    received = [await subscription.get() for _ in messages]

    assert [message.msg_id for message in received] == ["b-1", "a-1", "a-2"]
    assert [
        message.source_sequence for message in received if message.source == "source-a"
    ] == [1, 2]
    await bus.stop()


@pytest.mark.asyncio
async def test_same_source_sequence_one_to_one_hundred_stays_strictly_ordered() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(config("ordered", capacity=100))
    for sequence in range(1, 101):
        await bus.publish(
            FakeMessage(
                f"message-{sequence}", priority=sequence % 101, source_sequence=sequence
            )
        )
    received = [await subscription.get() for _ in range(100)]
    assert [message.source_sequence for message in received] == list(range(1, 101))
    await bus.stop()


@pytest.mark.asyncio
async def test_coalescing_keeps_latest_message_and_bounded_size() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "snapshots",
            capacity=2,
            policy=OverflowPolicy.COALESCE,
            coalesce_key=lambda message: message.group,
        )
    )
    for sequence in range(101):
        await bus.publish(
            FakeMessage(
                f"snapshot-{sequence}", source_sequence=sequence, group="same-window"
            )
        )

    latest = await subscription.get()
    metrics = subscription.metrics()
    assert latest.msg_id == "snapshot-100"
    assert metrics.queue_size == 0
    assert metrics.enqueued == 1
    assert metrics.coalesced == 100
    await bus.stop()


@pytest.mark.asyncio
async def test_coalesce_policy_rejects_new_key_when_capacity_is_full() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "snapshots",
            capacity=1,
            policy=OverflowPolicy.COALESCE,
            coalesce_key=lambda message: message.group,
        )
    )
    await bus.publish(FakeMessage("first", group="one"))
    report = await bus.publish(FakeMessage("second", group="two"))
    assert report.rejected == ("snapshots",)
    assert subscription.qsize() == 1
    assert (await subscription.get()).msg_id == "first"
    await bus.stop()


@pytest.mark.asyncio
async def test_rejecting_full_subscriber_does_not_block_healthy_subscriber() -> None:
    bus = await started_bus()
    full = bus.subscribe(config("full", capacity=1))
    healthy = bus.subscribe(config("healthy", capacity=2))
    await bus.publish(FakeMessage("first"))
    await healthy.get()

    report = await bus.publish(FakeMessage("second"))

    assert report.rejected == ("full",)
    assert (await healthy.get()).msg_id == "second"
    assert (await full.get()).msg_id == "first"
    await bus.stop()


@pytest.mark.asyncio
async def test_drop_policy_is_explicit_and_counted() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config("debug", capacity=1, policy=OverflowPolicy.DROP)
    )
    await bus.publish(FakeMessage("first"))
    report = await bus.publish(FakeMessage("second"))
    assert report.deliveries[0].outcome is DeliveryOutcome.DROPPED
    assert subscription.metrics().dropped == 1
    assert (await subscription.get()).msg_id == "first"
    await bus.stop()


@pytest.mark.asyncio
async def test_wait_policy_resumes_after_consumer_makes_space() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "facts",
            capacity=1,
            policy=OverflowPolicy.WAIT,
            wait_timeout_seconds=1,
        )
    )
    await bus.publish(FakeMessage("first"))
    pending_publish = asyncio.create_task(bus.publish(FakeMessage("second")))
    for _ in range(10):
        if subscription.metrics().wait_count == 1:
            break
        await asyncio.sleep(0)
    assert not pending_publish.done()
    assert subscription.metrics().wait_count == 1
    assert (await subscription.get()).msg_id == "first"
    report = await pending_publish
    assert report.deliveries[0].outcome is DeliveryOutcome.ENQUEUED
    assert (await subscription.get()).msg_id == "second"
    await bus.stop()


@pytest.mark.asyncio
async def test_wait_policy_times_out_with_stable_queue_full_error() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "commands",
            capacity=1,
            policy=OverflowPolicy.WAIT,
            wait_timeout_seconds=0.01,
        )
    )
    await subscription.offer(FakeMessage("first"))
    with pytest.raises(EventQueueFullError) as captured:
        await subscription.offer(FakeMessage("second"))
    assert captured.value.code == "EVENT_QUEUE_FULL"
    assert subscription.metrics().rejected == 1
    await bus.stop()


@pytest.mark.asyncio
async def test_closing_subscription_releases_waiting_publisher_as_closed() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "closing",
            capacity=1,
            policy=OverflowPolicy.WAIT,
            wait_timeout_seconds=1,
        )
    )
    await bus.publish(FakeMessage("first"))
    pending = asyncio.create_task(bus.publish(FakeMessage("second")))
    for _ in range(10):
        if subscription.metrics().wait_count:
            break
        await asyncio.sleep(0)
    await subscription.close()
    report = await pending
    assert report.deliveries[0].outcome is DeliveryOutcome.CLOSED
    await bus.stop()


@pytest.mark.asyncio
async def test_coalesce_key_failure_is_rejected_without_escaping_publish() -> None:
    bus = await started_bus()

    def broken_key(message: object) -> str:
        raise RuntimeError(f"cannot key {type(message).__name__}")

    subscription = bus.subscribe(config("broken-key", coalesce_key=broken_key))
    report = await bus.publish(FakeMessage("message"))
    assert report.rejected == ("broken-key",)
    assert subscription.metrics().rejected == 1
    await bus.stop()


@pytest.mark.asyncio
async def test_unsubscribe_closes_waiters_and_unknown_ids_are_explicit() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(config("temporary"))
    waiter = asyncio.create_task(subscription.get())
    await asyncio.sleep(0)
    await bus.unsubscribe("temporary")
    with pytest.raises(SubscriptionClosedError):
        await waiter
    with pytest.raises(KeyError, match="unknown subscription"):
        bus.subscription("temporary")
    with pytest.raises(KeyError, match="unknown subscription"):
        await bus.unsubscribe("temporary")
    await bus.stop()


@pytest.mark.asyncio
async def test_bus_lifecycle_stops_accepting_and_closes_all_subscriptions() -> None:
    bus = EventBus()
    subscription = bus.subscribe(config("lifecycle"))
    with pytest.raises(RuntimeError, match="not accepting"):
        await bus.publish(FakeMessage("before-start"))
    await bus.start()
    serving = asyncio.create_task(bus.serve())
    await bus.publish(FakeMessage("accepted"))
    await bus.quiesce()
    await serving
    with pytest.raises(RuntimeError, match="not accepting"):
        await bus.publish(FakeMessage("after-quiesce"))
    await bus.checkpoint()
    await bus.stop()
    assert (await subscription.get()).msg_id == "accepted"
    with pytest.raises(SubscriptionClosedError):
        await subscription.get()
    assert await subscription.offer(FakeMessage("closed")) is DeliveryOutcome.CLOSED


@pytest.mark.asyncio
async def test_publish_without_subscribers_returns_empty_report() -> None:
    bus = await started_bus()
    report = await bus.publish(FakeMessage("unobserved"))
    assert report.msg_id == "unobserved"
    assert report.deliveries == ()
    assert report.rejected == ()
    await bus.stop()


@pytest.mark.asyncio
async def test_ten_thousand_messages_have_no_loss_and_low_in_process_latency() -> None:
    bus = await started_bus()
    subscription = bus.subscribe(
        config(
            "load",
            capacity=256,
            policy=OverflowPolicy.WAIT,
            wait_timeout_seconds=2,
        )
    )
    total = 10_000
    latencies_ms: list[float] = []
    received_ids: list[str] = []

    async def consume() -> None:
        for _ in range(total):
            message = await subscription.get()
            received_ids.append(message.msg_id)
            latencies_ms.append((time.perf_counter() - message.created_at) * 1000)

    consumer = asyncio.create_task(consume())
    for sequence in range(total):
        await bus.publish(
            FakeMessage(
                f"load-{sequence}",
                source_sequence=sequence,
                created_at=time.perf_counter(),
            )
        )
    await consumer

    p95 = sorted(latencies_ms)[int(total * 0.95) - 1]
    assert len(received_ids) == total
    assert len(set(received_ids)) == total
    assert received_ids == [f"load-{sequence}" for sequence in range(total)]
    assert p95 < 100
    assert subscription.qsize() == 0
    await bus.stop()


def test_subscription_and_message_validation() -> None:
    with pytest.raises(ValueError, match="subscription_id"):
        config("")
    with pytest.raises(ValueError, match="message_types"):
        config("empty", message_types=frozenset())
    with pytest.raises(ValueError, match="capacity"):
        config("small", capacity=0)
    with pytest.raises(ValueError, match="wait_timeout"):
        config("timeout", wait_timeout_seconds=0)
    with pytest.raises(ValueError, match="coalesce_key"):
        config("coalesce", policy=OverflowPolicy.COALESCE)


@pytest.mark.asyncio
async def test_duplicate_subscription_and_invalid_message_are_rejected() -> None:
    bus = await started_bus()
    bus.subscribe(config("same"))
    with pytest.raises(ValueError, match="duplicate subscription"):
        bus.subscribe(config("same"))
    with pytest.raises(ValueError, match="identity"):
        await bus.publish(FakeMessage("", source=""))
    with pytest.raises(ValueError, match="priority"):
        await bus.publish(FakeMessage("bad-priority", priority=101))
    await bus.stop()
