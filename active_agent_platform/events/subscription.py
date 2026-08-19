"""One isolated bounded queue preserving FIFO order within each source."""

import asyncio
from dataclasses import dataclass

from active_agent_platform.events.models import (
    DeliveryOutcome,
    EventQueueFullError,
    OverflowPolicy,
    SubscriptionClosedError,
    SubscriptionConfig,
    SubscriptionMetrics,
)
from brain_kernel.ports import BusMessage


@dataclass(slots=True)
class _QueueEntry:
    sequence: int
    message: BusMessage
    coalesce_key: str | None


class Subscription:
    def __init__(self, config: SubscriptionConfig) -> None:
        self.config = config
        self._condition = asyncio.Condition()
        self._entries: list[_QueueEntry] = []
        self._coalesced_entries: dict[str, _QueueEntry] = {}
        self._sequence = 0
        self._closed = False
        self._enqueued = 0
        self._delivered = 0
        self._coalesced = 0
        self._filtered = 0
        self._dropped = 0
        self._rejected = 0
        self._wait_count = 0

    def accepts(self, message: BusMessage) -> bool:
        if message.msg_type not in self.config.message_types:
            self._filtered += 1
            return False
        if self.config.predicate is not None and not self.config.predicate(message):
            self._filtered += 1
            return False
        return True

    async def offer(self, message: BusMessage) -> DeliveryOutcome:
        key = self.config.coalesce_key(message) if self.config.coalesce_key else None
        async with self._condition:
            if self._closed:
                return DeliveryOutcome.CLOSED
            if key is not None and key in self._coalesced_entries:
                self._coalesced_entries[key].message = message
                self._coalesced += 1
                self._condition.notify_all()
                return DeliveryOutcome.COALESCED
            if len(self._entries) >= self.config.capacity:
                outcome = await self._handle_full_queue()
                if outcome is not None:
                    return outcome
            if self._closed:
                return DeliveryOutcome.CLOSED
            self._sequence += 1
            entry = _QueueEntry(self._sequence, message, key)
            self._entries.append(entry)
            if key is not None:
                self._coalesced_entries[key] = entry
            self._enqueued += 1
            self._condition.notify_all()
            return DeliveryOutcome.ENQUEUED

    async def get(self) -> BusMessage:
        async with self._condition:
            await self._condition.wait_for(lambda: bool(self._entries) or self._closed)
            if not self._entries:
                raise SubscriptionClosedError(self.config.subscription_id)
            entry = self._select_next_entry()
            self._entries.remove(entry)
            if (
                entry.coalesce_key is not None
                and self._coalesced_entries.get(entry.coalesce_key) is entry
            ):
                del self._coalesced_entries[entry.coalesce_key]
            self._delivered += 1
            self._condition.notify_all()
            return entry.message

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    def qsize(self) -> int:
        return len(self._entries)

    def metrics(self) -> SubscriptionMetrics:
        return SubscriptionMetrics(
            enqueued=self._enqueued,
            delivered=self._delivered,
            coalesced=self._coalesced,
            filtered=self._filtered,
            dropped=self._dropped,
            rejected=self._rejected,
            wait_count=self._wait_count,
            queue_size=len(self._entries),
        )

    def record_rejected(self) -> None:
        self._rejected += 1

    async def _handle_full_queue(self) -> DeliveryOutcome | None:
        policy = self.config.overflow_policy
        if policy is OverflowPolicy.DROP:
            self._dropped += 1
            return DeliveryOutcome.DROPPED
        if policy in {OverflowPolicy.REJECT, OverflowPolicy.COALESCE}:
            self._rejected += 1
            raise EventQueueFullError(self.config.subscription_id)

        self._wait_count += 1
        try:
            await asyncio.wait_for(
                self._condition.wait_for(
                    lambda: len(self._entries) < self.config.capacity or self._closed
                ),
                timeout=self.config.wait_timeout_seconds,
            )
        except TimeoutError as error:
            self._rejected += 1
            raise EventQueueFullError(self.config.subscription_id) from error
        return None

    def _select_next_entry(self) -> _QueueEntry:
        first_by_source: dict[str, _QueueEntry] = {}
        for entry in self._entries:
            first_by_source.setdefault(entry.message.source, entry)
        return max(
            first_by_source.values(),
            key=lambda entry: (entry.message.priority, -entry.sequence),
        )
