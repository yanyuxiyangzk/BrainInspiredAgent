from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from active_agent_platform import (
    CognitiveCoordinator,
    CompletionCondition,
    ConditionOperator,
    CycleOutcome,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    GoalSnapshot,
    MemoryContextSnapshot,
    StimulusOutcome,
    WorldModel,
    WorldSnapshot,
)
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator

NOW = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
CYCLE_1 = UUID("018f0000-0000-7000-8000-000000000101")
CYCLE_2 = UUID("018f0000-0000-7000-8000-000000000102")


def stimulus(
    sequence: int,
    *,
    msg_type: str = "attention.salient_event",
    priority: int = 50,
    dedup_key: str | None = None,
) -> EventEnvelope:
    msg_id = f"018f0000-0000-7000-8000-{sequence:012d}"
    return EventEnvelope(
        msg_id=msg_id,
        msg_type=msg_type,
        source="test.source",
        occurred_at=NOW + timedelta(milliseconds=sequence),
        published_at=NOW + timedelta(milliseconds=sequence),
        priority=priority,
        correlation_id=f"018f0000-0000-7000-8001-{sequence:012d}",
        dedup_key=dedup_key or f"stimulus:{sequence}",
        payload={
            "event_type": msg_type,
            "stimulus_id": f"stimulus:{sequence}",
            "data": {"sequence": sequence},
            "data_quality": "VALID",
        },
    )


def snapshots(
    clock: FakeClock, *, domains: tuple[str, ...] = ("primary",)
) -> tuple[WorldSnapshot, GoalSnapshot, MemoryContextSnapshot]:
    world = WorldModel(clock).snapshot
    goals = tuple(
        GoalDefinition(
            f"goal.{index}",
            1,
            100 - index,
            domain,
            NOW + timedelta(hours=1),
            GoalBudget(100, 10, "USD", 30),
            (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
        )
        for index, domain in enumerate(domains)
    )
    goal_snapshot = GoalPolicy(clock, goals).evaluate({"done": False})
    memory = MemoryContextSnapshot(3, clock.now(), {"recent": [{"id": "episode-1"}]})
    return world, goal_snapshot, memory


def coordinator(clock: FakeClock, *, active: int = 1, batch: int = 32, pending: int = 256) -> CognitiveCoordinator:
    return CognitiveCoordinator(
        clock,
        FakeUuidGenerator((CYCLE_1, CYCLE_2)),
        merge_window_seconds=1,
        max_pending=pending,
        max_stimuli_per_cycle=batch,
        max_active_cycles=active,
    )


def test_coordinator_merges_window_and_freezes_cycle_snapshots() -> None:
    clock = FakeClock(NOW)
    service = coordinator(clock)
    first = stimulus(1, priority=20)
    focus = stimulus(2, priority=90)
    assert service.submit(first).outcome is StimulusOutcome.ACCEPTED
    assert service.submit(focus).outcome is StimulusOutcome.ACCEPTED
    world, goals, memory = snapshots(clock)
    assert service.form_cycle(world, goals, memory).outcome is CycleOutcome.WAITING
    clock.advance(1)

    decision = service.form_cycle(world, goals, memory)
    assert decision.outcome is CycleOutcome.CREATED
    cycle = decision.cycle
    assert cycle is not None
    assert cycle.cognitive_cycle_id == str(CYCLE_1)
    assert tuple(item.msg_id for item in cycle.stimuli) == (first.msg_id, focus.msg_id)
    assert cycle.focus_msg_id == focus.msg_id
    assert cycle.world_snapshot is world and cycle.goal_snapshot is goals
    assert cycle.memory_snapshot is memory
    assert cycle.selected_goal_ids == ("goal.0",)
    assert cycle.correlation_ids == (first.correlation_id, focus.correlation_id)
    with pytest.raises(TypeError):
        cast(dict[str, object], memory.entries)["x"] = 1
    recent = cast(tuple[object, ...], memory.entries["recent"])
    with pytest.raises(TypeError):
        cast(dict[str, object], recent[0])["id"] = "changed"


def test_late_stimulus_enters_next_cycle_after_completion() -> None:
    clock = FakeClock(NOW)
    service = coordinator(clock)
    world, goals, memory = snapshots(clock)
    service.submit(stimulus(1))
    first = service.form_cycle(world, goals, memory, force=True).cycle
    assert first is not None
    service.submit(stimulus(2))
    assert service.form_cycle(world, goals, memory, force=True).outcome is CycleOutcome.BUSY
    assert service.complete(first.cognitive_cycle_id)
    assert not service.complete(first.cognitive_cycle_id)
    second = service.form_cycle(world, goals, memory, force=True).cycle
    assert second is not None and second.cognitive_cycle_id == str(CYCLE_2)
    assert tuple(item.msg_id for item in second.stimuli) == (stimulus(2).msg_id,)


def test_conflict_domain_blocks_cycle_while_unrelated_domain_can_continue() -> None:
    clock = FakeClock(NOW)
    service = coordinator(clock, active=2)
    world, first_goals, memory = snapshots(clock)
    service.submit(stimulus(1))
    first = service.form_cycle(world, first_goals, memory, force=True).cycle
    assert first is not None

    service.submit(stimulus(2))
    assert service.form_cycle(world, first_goals, memory, force=True).outcome is CycleOutcome.CONFLICT
    _, independent_goals, _ = snapshots(clock, domains=("other",))
    second = service.form_cycle(world, independent_goals, memory, force=True)
    assert second.outcome is CycleOutcome.CREATED


def test_stimulus_validation_dedup_capacity_batch_and_metrics() -> None:
    clock = FakeClock(NOW)
    service = coordinator(clock, batch=1, pending=2)
    accepted = stimulus(1, dedup_key="same")
    assert service.submit(accepted).outcome is StimulusOutcome.ACCEPTED
    assert service.submit(accepted).outcome is StimulusOutcome.DUPLICATE
    assert service.submit(stimulus(2, dedup_key="same")).outcome is StimulusOutcome.DUPLICATE
    assert service.submit(stimulus(3, msg_type="perception.snapshot")).outcome is StimulusOutcome.INVALID
    assert service.submit(stimulus(4)).outcome is StimulusOutcome.ACCEPTED
    assert service.submit(stimulus(5)).outcome is StimulusOutcome.CAPACITY
    world, goals, memory = snapshots(clock)
    cycle = service.form_cycle(world, goals, memory, force=True).cycle
    assert cycle is not None and len(cycle.stimuli) == 1
    metrics = service.metrics()
    assert (metrics.accepted, metrics.duplicate, metrics.invalid, metrics.capacity) == (2, 2, 1, 1)
    assert metrics.cycles_created == metrics.pending == metrics.active == 1


def test_coordinator_waits_without_stimuli_or_available_goal() -> None:
    clock = FakeClock(NOW)
    service = coordinator(clock)
    world, goals, memory = snapshots(clock)
    assert service.form_cycle(world, goals, memory).outcome is CycleOutcome.WAITING
    service.submit(stimulus(1))
    empty_goals = GoalPolicy(clock, ()).evaluate({})
    assert service.form_cycle(world, empty_goals, memory, force=True).outcome is CycleOutcome.CONFLICT
    assert service.metrics().pending == 1


def test_coordinator_configuration_and_memory_validation() -> None:
    clock = FakeClock(NOW)
    uuid = FakeUuidGenerator((CYCLE_1,))
    with pytest.raises(ValueError):
        CognitiveCoordinator(clock, uuid, merge_window_seconds=-1)
    with pytest.raises(ValueError):
        CognitiveCoordinator(clock, uuid, max_pending=0)
    with pytest.raises(ValueError):
        CognitiveCoordinator(clock, uuid, max_pending=1, max_stimuli_per_cycle=2)
    with pytest.raises(ValueError):
        MemoryContextSnapshot(-1, NOW, {})
    with pytest.raises(ValueError):
        MemoryContextSnapshot(0, NOW.replace(tzinfo=None), {})
