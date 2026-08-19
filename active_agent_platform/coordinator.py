"""Bounded coordinator that freezes one consistent cognitive-cycle context."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from active_agent_platform.cognition import WorldSnapshot
from active_agent_platform.events import EventEnvelope
from active_agent_platform.goals import GoalSnapshot, GoalStatus
from brain_kernel.ports import Clock, UuidGenerator


class StimulusOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"
    CAPACITY = "CAPACITY"


class CycleOutcome(StrEnum):
    CREATED = "CREATED"
    WAITING = "WAITING"
    BUSY = "BUSY"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class MemoryContextSnapshot:
    version: int
    created_at: datetime
    entries: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("memory snapshot version must be non-negative")
        object.__setattr__(self, "created_at", _aware_utc(self.created_at))
        object.__setattr__(self, "entries", _freeze_mapping(self.entries))


@dataclass(frozen=True, slots=True)
class StimulusDecision:
    outcome: StimulusOutcome
    msg_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CognitiveCycle:
    cognitive_cycle_id: str
    created_at: datetime
    stimuli: tuple[EventEnvelope, ...]
    focus_msg_id: str
    world_snapshot: WorldSnapshot
    goal_snapshot: GoalSnapshot
    memory_snapshot: MemoryContextSnapshot
    selected_goal_ids: tuple[str, ...]
    conflict_domains: tuple[str, ...]
    correlation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CycleDecision:
    outcome: CycleOutcome
    cycle: CognitiveCycle | None
    reason: str


@dataclass(frozen=True, slots=True)
class CoordinatorMetrics:
    accepted: int
    duplicate: int
    invalid: int
    capacity: int
    cycles_created: int
    pending: int
    active: int


@dataclass(frozen=True, slots=True)
class _PendingStimulus:
    event: EventEnvelope
    accepted_at: float


class CognitiveCoordinator:
    """Merge stimuli and lock immutable snapshots for bounded planning cycles."""

    _SUPPORTED_TYPES = frozenset(
        {"attention.salient_event", "command.received", "schedule.triggered", "goal.changed"}
    )

    def __init__(
        self,
        clock: Clock,
        uuid: UuidGenerator,
        *,
        merge_window_seconds: float = 0.25,
        max_pending: int = 256,
        max_stimuli_per_cycle: int = 32,
        max_active_cycles: int = 1,
    ) -> None:
        if merge_window_seconds < 0:
            raise ValueError("merge_window_seconds must be non-negative")
        if min(max_pending, max_stimuli_per_cycle, max_active_cycles) < 1:
            raise ValueError("coordinator capacities must be positive")
        if max_stimuli_per_cycle > max_pending:
            raise ValueError("cycle stimulus limit cannot exceed pending capacity")
        _aware_utc(clock.now())
        self._clock = clock
        self._uuid = uuid
        self._merge_window = merge_window_seconds
        self._max_pending = max_pending
        self._max_batch = max_stimuli_per_cycle
        self._max_active = max_active_cycles
        self._pending: list[_PendingStimulus] = []
        self._seen_msg_ids: set[str] = set()
        self._seen_dedup_keys: set[str] = set()
        self._active: dict[str, CognitiveCycle] = {}
        self._counts = {outcome: 0 for outcome in StimulusOutcome}
        self._cycles_created = 0

    def submit(self, event: EventEnvelope) -> StimulusDecision:
        if event.msg_type not in self._SUPPORTED_TYPES:
            return self._submission(StimulusOutcome.INVALID, event, "unsupported stimulus type")
        if event.msg_id in self._seen_msg_ids or event.dedup_key in self._seen_dedup_keys:
            return self._submission(StimulusOutcome.DUPLICATE, event, "stimulus already accepted")
        if len(self._pending) >= self._max_pending:
            return self._submission(StimulusOutcome.CAPACITY, event, "pending stimulus capacity reached")
        self._seen_msg_ids.add(event.msg_id)
        self._seen_dedup_keys.add(event.dedup_key)
        self._pending.append(_PendingStimulus(event, self._clock.monotonic()))
        return self._submission(StimulusOutcome.ACCEPTED, event, "stimulus queued")

    def form_cycle(
        self,
        world_snapshot: WorldSnapshot,
        goal_snapshot: GoalSnapshot,
        memory_snapshot: MemoryContextSnapshot,
        *,
        force: bool = False,
    ) -> CycleDecision:
        if not self._pending:
            return CycleDecision(CycleOutcome.WAITING, None, "no pending stimuli")
        if len(self._active) >= self._max_active:
            return CycleDecision(CycleOutcome.BUSY, None, "planning concurrency limit reached")
        oldest = self._pending[0]
        if not force and self._clock.monotonic() - oldest.accepted_at < self._merge_window:
            return CycleDecision(CycleOutcome.WAITING, None, "merge window remains open")

        selected_goals, domains = self._available_goals(goal_snapshot)
        if not selected_goals:
            return CycleDecision(CycleOutcome.CONFLICT, None, "no goal is available outside active domains")

        pending_batch = self._pending[: self._max_batch]
        stimuli = tuple(item.event for item in pending_batch)
        focus = min(stimuli, key=lambda event: (-event.priority, event.occurred_at, event.msg_id))
        now = _aware_utc(self._clock.now())
        cycle_id = str(self._uuid.new())
        correlations = tuple(dict.fromkeys(event.correlation_id for event in stimuli))
        cycle = CognitiveCycle(
            cycle_id,
            now,
            stimuli,
            focus.msg_id,
            world_snapshot,
            goal_snapshot,
            memory_snapshot,
            selected_goals,
            domains,
            correlations,
        )
        del self._pending[: len(pending_batch)]
        self._active[cycle_id] = cycle
        self._cycles_created += 1
        return CycleDecision(CycleOutcome.CREATED, cycle, "cognitive cycle created")

    def complete(self, cognitive_cycle_id: str) -> bool:
        return self._active.pop(cognitive_cycle_id, None) is not None

    def metrics(self) -> CoordinatorMetrics:
        return CoordinatorMetrics(
            self._counts[StimulusOutcome.ACCEPTED],
            self._counts[StimulusOutcome.DUPLICATE],
            self._counts[StimulusOutcome.INVALID],
            self._counts[StimulusOutcome.CAPACITY],
            self._cycles_created,
            len(self._pending),
            len(self._active),
        )

    def _available_goals(self, snapshot: GoalSnapshot) -> tuple[tuple[str, ...], tuple[str, ...]]:
        occupied = {
            domain for cycle in self._active.values() for domain in cycle.conflict_domains
        }
        goal_ids: list[str] = []
        domains: list[str] = []
        for goal_id in snapshot.selected_goal_ids:
            evaluation = snapshot.get(goal_id)
            if evaluation is None or evaluation.status is not GoalStatus.AVAILABLE:
                continue
            domain = evaluation.goal.conflict_domain
            if domain in occupied:
                continue
            goal_ids.append(goal_id)
            domains.append(domain)
        return tuple(goal_ids), tuple(domains)

    def _submission(
        self, outcome: StimulusOutcome, event: EventEnvelope, reason: str
    ) -> StimulusDecision:
        self._counts[outcome] += 1
        return StimulusDecision(outcome, event.msg_id, reason)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value
