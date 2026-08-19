"""Thalamus-style in-process event bus with isolated subscriptions."""

import asyncio

from active_agent_platform.events.contracts import EventSchemaRegistry
from active_agent_platform.events.models import (
    DeliveryOutcome,
    DeliveryResult,
    EventQueueFullError,
    PublishReport,
    SubscriptionConfig,
)
from active_agent_platform.events.subscription import Subscription
from brain_kernel.ports import BusMessage


class EventBus:
    name = "event_bus"

    def __init__(self, *, schema_registry: EventSchemaRegistry | None = None) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self._publish_lock = asyncio.Lock()
        self._serve_stopped = asyncio.Event()
        self._accepting = False
        self._schema_registry = schema_registry

    def subscribe(self, config: SubscriptionConfig) -> Subscription:
        if config.subscription_id in self._subscriptions:
            raise ValueError(f"duplicate subscription: {config.subscription_id}")
        subscription = Subscription(config)
        self._subscriptions[config.subscription_id] = subscription
        return subscription

    async def unsubscribe(self, subscription_id: str) -> None:
        try:
            subscription = self._subscriptions.pop(subscription_id)
        except KeyError as error:
            raise KeyError(f"unknown subscription: {subscription_id}") from error
        await subscription.close()

    async def publish(self, message: BusMessage) -> PublishReport:
        if not self._accepting:
            raise RuntimeError("event bus is not accepting messages")
        if self._schema_registry is not None:
            self._schema_registry.validate(message)  # type: ignore[arg-type]
        if not message.msg_id or not message.msg_type or not message.source:
            raise ValueError("message identity, type and source are required")
        if not 0 <= message.priority <= 100:
            raise ValueError("message priority must be between 0 and 100")

        async with self._publish_lock:
            candidates = tuple(self._subscriptions.values())
            tasks: list[tuple[Subscription, asyncio.Task[DeliveryOutcome]]] = []
            results: list[DeliveryResult] = []
            for subscription in candidates:
                try:
                    accepted = subscription.accepts(message)
                except Exception:  # noqa: BLE001 - isolate plugin-owned predicates.
                    subscription.record_rejected()
                    results.append(
                        DeliveryResult(
                            subscription.config.subscription_id, DeliveryOutcome.REJECTED
                        )
                    )
                    continue
                if accepted:
                    tasks.append(
                        (
                            subscription,
                            asyncio.create_task(
                                subscription.offer(message),
                                name=f"event-delivery:{subscription.config.subscription_id}",
                            ),
                        )
                    )
                else:
                    results.append(
                        DeliveryResult(
                            subscription.config.subscription_id, DeliveryOutcome.FILTERED
                        )
                    )
            if tasks:
                outcomes = await asyncio.gather(
                    *(task for _, task in tasks), return_exceptions=True
                )
                for (subscription, _), outcome in zip(tasks, outcomes, strict=True):
                    if isinstance(outcome, EventQueueFullError):
                        delivery = DeliveryOutcome.REJECTED
                    elif isinstance(outcome, BaseException):
                        subscription.record_rejected()
                        delivery = DeliveryOutcome.REJECTED
                    else:
                        delivery = outcome
                    results.append(DeliveryResult(subscription.config.subscription_id, delivery))
            results.sort(key=lambda result: result.subscription_id)
            return PublishReport(message.msg_id, tuple(results))

    def subscription(self, subscription_id: str) -> Subscription:
        try:
            return self._subscriptions[subscription_id]
        except KeyError as error:
            raise KeyError(f"unknown subscription: {subscription_id}") from error

    async def start(self) -> None:
        self._serve_stopped = asyncio.Event()
        self._accepting = True

    async def serve(self) -> None:
        await self._serve_stopped.wait()

    async def quiesce(self) -> None:
        self._accepting = False
        self._serve_stopped.set()

    async def checkpoint(self) -> None:
        return None

    async def stop(self) -> None:
        self._accepting = False
        self._serve_stopped.set()
        await asyncio.gather(
            *(subscription.close() for subscription in self._subscriptions.values())
        )
