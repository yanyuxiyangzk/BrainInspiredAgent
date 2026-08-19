"""Bounded, expiring working-memory projection with explicit rebuild provenance."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

from active_agent_platform.coordinator import MemoryContextSnapshot
from brain_kernel.ports import Clock


class MemoryOrigin(StrEnum):
    TRANSIENT = "TRANSIENT"
    REBUILT = "REBUILT"


class MemoryWriteOutcome(StrEnum):
    STORED = "STORED"
    DUPLICATE = "DUPLICATE"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"


@dataclass(frozen=True, slots=True)
class WorkingMemoryEntry:
    memory_id: str
    kind: str
    content: Mapping[str, object]
    importance: float
    created_at: datetime
    expires_at: datetime
    origin: MemoryOrigin = MemoryOrigin.TRANSIENT
    source_fact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.memory_id or not self.kind:
            raise ValueError("memory_id and kind must not be empty")
        if not 0 <= self.importance <= 1 or not isfinite(self.importance):
            raise ValueError("importance must be finite and between 0 and 1")
        created_at = _aware_utc(self.created_at)
        expires_at = _aware_utc(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.origin is MemoryOrigin.REBUILT and not self.source_fact_id:
            raise ValueError("rebuilt memory requires source_fact_id")
        if self.origin is MemoryOrigin.TRANSIENT and self.source_fact_id is not None:
            raise ValueError("transient memory cannot claim a persistent source fact")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "content", _freeze_mapping(self.content))


@dataclass(frozen=True, slots=True)
class RebuildFact:
    source_fact_id: str
    memory_id: str
    kind: str
    content: Mapping[str, object]
    importance: float
    created_at: datetime
    expires_at: datetime

    def to_entry(self) -> WorkingMemoryEntry:
        if not self.source_fact_id:
            raise ValueError("source_fact_id must not be empty")
        return WorkingMemoryEntry(
            self.memory_id,
            self.kind,
            self.content,
            self.importance,
            self.created_at,
            self.expires_at,
            MemoryOrigin.REBUILT,
            self.source_fact_id,
        )


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    outcome: MemoryWriteOutcome
    memory_id: str
    evicted_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkingMemoryMetrics:
    size: int
    capacity: int
    stored: int
    duplicate: int
    rejected: int
    expired: int
    evicted: int
    rebuilds: int


class WorkingMemory:
    """Keep only bounded current context; persistent facts remain authoritative."""

    def __init__(self, clock: Clock, *, capacity: int = 100, default_ttl_seconds: float = 3600) -> None:
        if capacity < 1:
            raise ValueError("working-memory capacity must be positive")
        if default_ttl_seconds <= 0 or not isfinite(default_ttl_seconds):
            raise ValueError("default TTL must be finite and positive")
        _aware_utc(clock.now())
        self._clock = clock
        self._capacity = capacity
        self._default_ttl = default_ttl_seconds
        self._entries: dict[str, WorkingMemoryEntry] = {}
        self._version = 0
        self._stored = 0
        self._duplicates = 0
        self._rejected = 0
        self._expired = 0
        self._evicted = 0
        self._rebuilds = 0

    def remember(
        self,
        memory_id: str,
        kind: str,
        content: Mapping[str, object],
        *,
        importance: float = 0.5,
        ttl_seconds: float | None = None,
    ) -> MemoryWriteResult:
        now = _aware_utc(self._clock.now())
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        if ttl <= 0 or not isfinite(ttl):
            raise ValueError("TTL must be finite and positive")
        entry = WorkingMemoryEntry(
            memory_id,
            kind,
            content,
            importance,
            now,
            now + timedelta(seconds=ttl),
        )
        return self._insert(entry)

    def get(self, memory_id: str) -> WorkingMemoryEntry | None:
        self.reclaim()
        return self._entries.get(memory_id)

    def entries(self) -> tuple[WorkingMemoryEntry, ...]:
        self.reclaim()
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda entry: (-entry.importance, -entry.created_at.timestamp(), entry.memory_id),
            )
        )

    def reclaim(self) -> tuple[str, ...]:
        now = _aware_utc(self._clock.now())
        expired = tuple(
            memory_id
            for memory_id, entry in self._entries.items()
            if entry.expires_at <= now
        )
        if expired:
            for memory_id in expired:
                del self._entries[memory_id]
            self._expired += len(expired)
            self._version += 1
        return expired

    def clear(self) -> None:
        if self._entries:
            self._entries.clear()
            self._version += 1

    def rebuild(self, facts: Iterable[RebuildFact]) -> tuple[str, ...]:
        now = _aware_utc(self._clock.now())
        candidates: dict[str, WorkingMemoryEntry] = {}
        for fact in facts:
            entry = fact.to_entry()
            if entry.expires_at <= now:
                continue
            if entry.memory_id in candidates:
                raise ValueError("rebuild facts must have unique memory IDs")
            candidates[entry.memory_id] = entry
        retained = sorted(
            candidates.values(),
            key=lambda entry: (-entry.importance, -entry.created_at.timestamp(), entry.memory_id),
        )[: self._capacity]
        self._entries = {entry.memory_id: entry for entry in retained}
        self._version += 1
        self._rebuilds += 1
        return tuple(entry.memory_id for entry in retained)

    def snapshot(self) -> MemoryContextSnapshot:
        self.reclaim()
        return MemoryContextSnapshot(
            self._version,
            _aware_utc(self._clock.now()),
            {entry.memory_id: entry for entry in self.entries()},
        )

    def metrics(self) -> WorkingMemoryMetrics:
        self.reclaim()
        return WorkingMemoryMetrics(
            len(self._entries),
            self._capacity,
            self._stored,
            self._duplicates,
            self._rejected,
            self._expired,
            self._evicted,
            self._rebuilds,
        )

    def _insert(self, entry: WorkingMemoryEntry) -> MemoryWriteResult:
        self.reclaim()
        if entry.memory_id in self._entries:
            self._duplicates += 1
            return MemoryWriteResult(MemoryWriteOutcome.DUPLICATE, entry.memory_id)
        self._entries[entry.memory_id] = entry
        evicted: tuple[str, ...] = ()
        if len(self._entries) > self._capacity:
            victim = min(
                self._entries.values(),
                key=lambda item: (item.importance, item.created_at, item.memory_id),
            )
            del self._entries[victim.memory_id]
            evicted = (victim.memory_id,)
            self._evicted += 1
        self._version += 1
        if entry.memory_id in evicted:
            self._rejected += 1
            return MemoryWriteResult(MemoryWriteOutcome.REJECTED_BY_POLICY, entry.memory_id, evicted)
        self._stored += 1
        return MemoryWriteResult(MemoryWriteOutcome.STORED, entry.memory_id, evicted)


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
