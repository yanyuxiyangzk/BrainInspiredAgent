"""Configuration, outcomes and metrics for event subscriptions."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from brain_kernel.ports import BusMessage


class OverflowPolicy(StrEnum):
    WAIT = "WAIT"
    REJECT = "REJECT"
    DROP = "DROP"
    COALESCE = "COALESCE"


class DeliveryOutcome(StrEnum):
    ENQUEUED = "ENQUEUED"
    COALESCED = "COALESCED"
    FILTERED = "FILTERED"
    DROPPED = "DROPPED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class EventQueueFullError(RuntimeError):
    code = "EVENT_QUEUE_FULL"


class SubscriptionClosedError(RuntimeError):
    pass


MessagePredicate = Callable[[BusMessage], bool]
CoalesceKey = Callable[[BusMessage], str]


@dataclass(frozen=True, slots=True)
class SubscriptionConfig:
    subscription_id: str
    message_types: frozenset[str]
    capacity: int = 100
    overflow_policy: OverflowPolicy = OverflowPolicy.REJECT
    predicate: MessagePredicate | None = None
    coalesce_key: CoalesceKey | None = None
    wait_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.subscription_id:
            raise ValueError("subscription_id must not be empty")
        if not self.message_types:
            raise ValueError("message_types must not be empty")
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        if self.wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        if self.overflow_policy is OverflowPolicy.COALESCE and self.coalesce_key is None:
            raise ValueError("COALESCE policy requires coalesce_key")


@dataclass(frozen=True, slots=True)
class SubscriptionMetrics:
    enqueued: int
    delivered: int
    coalesced: int
    filtered: int
    dropped: int
    rejected: int
    wait_count: int
    queue_size: int


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    subscription_id: str
    outcome: DeliveryOutcome


@dataclass(frozen=True, slots=True)
class PublishReport:
    msg_id: str
    deliveries: tuple[DeliveryResult, ...]

    @property
    def rejected(self) -> tuple[str, ...]:
        return tuple(
            result.subscription_id
            for result in self.deliveries
            if result.outcome is DeliveryOutcome.REJECTED
        )
