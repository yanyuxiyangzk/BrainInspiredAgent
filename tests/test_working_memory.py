from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from active_agent_platform import (
    MemoryOrigin,
    MemoryWriteOutcome,
    RebuildFact,
    WorkingMemory,
    WorkingMemoryEntry,
)
from active_agent_platform.foundation import FakeClock

NOW = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)


def test_capacity_100_evicts_oldest_of_equal_importance() -> None:
    clock = FakeClock(NOW)
    memory = WorkingMemory(clock, capacity=100)
    for index in range(101):
        result = memory.remember(f"item-{index:03d}", "event", {"index": index})
        assert result.outcome is MemoryWriteOutcome.STORED
        clock.advance(0.001)
    assert len(memory.entries()) == 100
    assert memory.get("item-000") is None
    assert memory.get("item-100") is not None
    assert memory.metrics().evicted == 1


def test_reclaim_expires_entries_before_importance_eviction() -> None:
    clock = FakeClock(NOW)
    memory = WorkingMemory(clock, capacity=2)
    memory.remember("high", "context", {}, importance=1.0, ttl_seconds=100)
    memory.remember("expiring", "context", {}, importance=0.9, ttl_seconds=1)
    clock.advance(2)
    result = memory.remember("ordinary", "context", {}, importance=0.1)
    assert result.evicted_ids == ()
    assert memory.get("high") is not None
    assert memory.get("expiring") is None
    assert memory.metrics().expired == 1


def test_high_importance_survives_ordinary_capacity_pressure() -> None:
    clock = FakeClock(NOW)
    memory = WorkingMemory(clock, capacity=2)
    memory.remember("high", "goal", {}, importance=1.0)
    memory.remember("ordinary-old", "event", {}, importance=0.2)
    clock.advance(1)
    result = memory.remember("ordinary-new", "event", {}, importance=0.2)
    assert result.evicted_ids == ("ordinary-old",)
    assert memory.get("high") is not None
    rejected = memory.remember("low", "event", {}, importance=0.1)
    assert rejected.outcome is MemoryWriteOutcome.REJECTED_BY_POLICY
    assert rejected.evicted_ids == ("low",)


def test_new_instance_is_empty_and_rebuild_requires_persistent_provenance() -> None:
    clock = FakeClock(NOW)
    old = WorkingMemory(clock, capacity=2)
    old.remember("transient", "event", {"value": 1})
    restarted = WorkingMemory(clock, capacity=2)
    assert restarted.entries() == ()
    assert restarted.snapshot().version == 0

    retained = restarted.rebuild(
        (
            RebuildFact("episode-1", "rebuilt", "episode", {"value": 2}, 0.8, NOW, NOW + timedelta(hours=1)),
            RebuildFact("episode-old", "expired", "episode", {}, 1.0, NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
        )
    )
    assert retained == ("rebuilt",)
    entry = restarted.get("rebuilt")
    assert entry is not None and entry.origin is MemoryOrigin.REBUILT
    assert entry.source_fact_id == "episode-1"
    assert restarted.get("transient") is None


def test_snapshot_is_deeply_immutable_and_compatible_with_coordinator() -> None:
    memory = WorkingMemory(FakeClock(NOW))
    memory.remember("one", "event", {"nested": [{"value": 1}]})
    snapshot = memory.snapshot()
    entry = cast(WorkingMemoryEntry, snapshot.entries["one"])
    with pytest.raises(TypeError):
        cast(dict[str, object], snapshot.entries)["two"] = entry
    nested = cast(tuple[object, ...], entry.content["nested"])
    with pytest.raises(TypeError):
        cast(dict[str, object], nested[0])["value"] = 2


def test_duplicate_clear_ordering_and_metrics() -> None:
    clock = FakeClock(NOW)
    memory = WorkingMemory(clock)
    assert memory.remember("one", "event", {}).outcome is MemoryWriteOutcome.STORED
    assert memory.remember("one", "event", {}).outcome is MemoryWriteOutcome.DUPLICATE
    clock.advance(1)
    memory.remember("important", "event", {}, importance=0.9)
    assert tuple(entry.memory_id for entry in memory.entries()) == ("important", "one")
    metrics = memory.metrics()
    assert (metrics.stored, metrics.duplicate, metrics.size) == (2, 1, 2)
    memory.clear()
    assert memory.entries() == ()
    memory.clear()


def test_rebuild_is_atomic_bounded_and_rejects_duplicate_ids() -> None:
    clock = FakeClock(NOW)
    memory = WorkingMemory(clock, capacity=1)
    high = RebuildFact("fact-high", "high", "episode", {}, 1.0, NOW, NOW + timedelta(hours=1))
    low = RebuildFact("fact-low", "low", "episode", {}, 0.1, NOW, NOW + timedelta(hours=1))
    assert memory.rebuild((low, high)) == ("high",)
    assert memory.metrics().rebuilds == 1
    duplicate = RebuildFact("other-fact", "high", "episode", {}, 0.5, NOW, NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        memory.rebuild((high, duplicate))


def test_working_memory_validation() -> None:
    clock = FakeClock(NOW)
    with pytest.raises(ValueError):
        WorkingMemory(clock, capacity=0)
    with pytest.raises(ValueError):
        WorkingMemory(clock, default_ttl_seconds=0)
    memory = WorkingMemory(clock)
    with pytest.raises(ValueError):
        memory.remember("id", "kind", {}, ttl_seconds=0)
    with pytest.raises(ValueError):
        memory.remember("id", "kind", {}, importance=2)
    with pytest.raises(ValueError):
        WorkingMemoryEntry("", "kind", {}, 0.5, NOW, NOW + timedelta(1))
    with pytest.raises(ValueError):
        WorkingMemoryEntry("id", "kind", {}, 0.5, NOW, NOW)
    with pytest.raises(ValueError):
        WorkingMemoryEntry("id", "kind", {}, 0.5, NOW, NOW + timedelta(1), MemoryOrigin.REBUILT)
    with pytest.raises(ValueError):
        WorkingMemoryEntry("id", "kind", {}, 0.5, NOW, NOW + timedelta(1), source_fact_id="fake")
    with pytest.raises(ValueError):
        RebuildFact("", "id", "kind", {}, 0.5, NOW, NOW + timedelta(1)).to_entry()
