"""L-009 tests: iteration/failure/checkpoint/factor hooks, summaries, search-state wiring.

架构 §6：Hooks 是 Loop 事件订阅者，不是隐藏控制流。核心验收是幂等：同一
幂等键的事件重复投递对订阅者可见次数为一；Hook 异常不得影响 Loop 主流程；
L-004 搜索状态经 checkpoint 指针跨重启恢复。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_hooks import (
    FactorCoverageCollector,
    FactorFailureModeCollector,
    FactorHookBus,
    FactorHookEvent,
    FactorIterationSummaryCollector,
)
from domain_sdk.factor_loop import FactorDiscoveryLoop, FactorLoopProfile, FactorLoopStatus

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, tzinfo=UTC)
SEARCH_STATE = {
    "format": 1,
    "rounds": 3,
    "momentum": 0.25,
    "exploration_share": 0.4,
    "window_step": 2,
    "zero_accept_streak": 1,
    "group_weights": {"mutate": 0.4},
}


def make_loop(
    database: SQLiteDatabase,
    clock: FakeClock,
    pointer: Path | None,
    hooks: FactorHookBus | None = None,
    **profile_overrides: Any,
) -> FactorDiscoveryLoop:
    values: dict[str, Any] = {"max_consecutive_failures": 2}
    values.update(profile_overrides)
    return FactorDiscoveryLoop(
        database,
        clock,
        FactorLoopProfile("factor.discovery", "1.0.0", **values),
        checkpoint_path=pointer,
        hooks=hooks,
    )


class CountingHook:
    def __init__(self, name: str = "counting", *, explode: bool = False) -> None:
        self.name = name
        self.calls: list[str] = []
        self.explode = explode

    async def handle(self, payload: Any) -> None:
        self.calls.append(payload.idempotency_key)
        if self.explode:
            raise RuntimeError("hook exploded")


# --------------------------------------------------------------------------- 总线幂等


async def test_bus_delivers_once_per_idempotency_key() -> None:
    bus = FactorHookBus("factor.discovery", "1.0.0")
    hook = CountingHook()
    bus.register(FactorHookEvent.ITERATION_COMPLETED, hook)
    first = await bus.emit(
        FactorHookEvent.ITERATION_COMPLETED, iteration=1, digest="sha256:a", details={}
    )
    replay = await bus.emit(
        FactorHookEvent.ITERATION_COMPLETED, iteration=1, digest="sha256:a", details={}
    )
    assert first == 1
    assert replay == 0  # 幂等：同键重复投递是空操作
    assert hook.calls == ["iteration.completed:factor.discovery:1.0.0:1:sha256:a"]


async def test_different_iterations_and_events_deliver_independently() -> None:
    bus = FactorHookBus("p", "1.0.0")
    hook = CountingHook()
    for event in (FactorHookEvent.ITERATION_COMPLETED, FactorHookEvent.CHECKPOINT_COMMITTED):
        bus.register(event, hook)
        for iteration in (1, 2):
            assert (
                await bus.emit(event, iteration=iteration, digest="d", details={})
            ) == 1
    assert len(hook.calls) == 4


async def test_hook_failure_never_breaks_delivery() -> None:
    bus = FactorHookBus("p", "1.0.0")
    bad = CountingHook("bad", explode=True)
    good = CountingHook("good")
    bus.register(FactorHookEvent.FACTOR_ACCEPTED, bad)
    bus.register(FactorHookEvent.FACTOR_ACCEPTED, good)
    delivered = await bus.emit(FactorHookEvent.FACTOR_ACCEPTED, iteration=1, digest="d", details={})
    assert delivered == 1  # good 仍被投递
    assert len(good.calls) == 1
    assert bus.errors and "hook exploded" in bus.errors[0][1]


async def test_duplicate_registration_is_rejected() -> None:
    bus = FactorHookBus("p", "1.0.0")
    bus.register(FactorHookEvent.ITERATION_FAILED, CountingHook())
    with pytest.raises(ValueError, match="registered"):
        bus.register(FactorHookEvent.ITERATION_FAILED, CountingHook())


# --------------------------------------------------------------------------- 内置摘要


async def test_iteration_summary_collector_counts_both_outcomes() -> None:
    bus = FactorHookBus("p", "1.0.0")
    collector = FactorIterationSummaryCollector()
    bus.register(FactorHookEvent.ITERATION_COMPLETED, collector)
    bus.register(FactorHookEvent.ITERATION_FAILED, collector)
    for iteration in (1, 2):
        await bus.emit(
            FactorHookEvent.ITERATION_COMPLETED,
            iteration=iteration,
            digest="d",
            details={"candidates": 10, "accepted": 2},
        )
    await bus.emit(
        FactorHookEvent.ITERATION_FAILED, iteration=3, digest="d", details={"reason": "boom"}
    )
    await bus.emit(
        FactorHookEvent.ITERATION_COMPLETED,  # 同键重放：不得重复计数
        iteration=2,
        digest="d",
        details={"candidates": 10, "accepted": 2},
    )
    summary = collector.summary()
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["candidates_total"] == 20
    assert summary["accepted_total"] == 4
    assert summary["last_iteration"] == 3


async def test_failure_mode_collector_aggregates_modes() -> None:
    bus = FactorHookBus("p", "1.0.0")
    collector = FactorFailureModeCollector()
    bus.register(FactorHookEvent.ITERATION_FAILED, collector)
    await bus.emit(
        FactorHookEvent.ITERATION_FAILED,
        iteration=1,
        digest="d",
        details={"failure_modes": ["LOW_IC", "LOW_IC", "HIGH_TURNOVER"]},
    )
    await bus.emit(
        FactorHookEvent.ITERATION_FAILED,
        iteration=2,
        digest="d",
        details={"failure_modes": ["LOW_IC"]},
    )
    assert collector.summary() == {"LOW_IC": 3, "HIGH_TURNOVER": 1}


async def test_factor_coverage_collector_counts_by_strategy() -> None:
    bus = FactorHookBus("p", "1.0.0")
    collector = FactorCoverageCollector()
    bus.register(FactorHookEvent.FACTOR_ACCEPTED, collector)
    for index, strategy in enumerate(("mutate", "mutate", "random_explore")):
        await bus.emit(
            FactorHookEvent.FACTOR_ACCEPTED,
            iteration=1,
            digest=f"sha256:{index}",
            details={"strategy": strategy},
        )
    assert collector.summary() == {"total": 3, "mutate": 2, "random_explore": 1}


# --------------------------------------------------------------------------- Loop 接线


async def test_loop_emits_checkpoint_iteration_and_factor_hooks(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    bus = FactorHookBus("factor.discovery", "1.0.0")
    summary = FactorIterationSummaryCollector()
    coverage = FactorCoverageCollector()
    checkpoints = CountingHook("checkpoints")
    bus.register(FactorHookEvent.ITERATION_COMPLETED, summary)
    bus.register(FactorHookEvent.ITERATION_FAILED, summary)
    bus.register(FactorHookEvent.FACTOR_ACCEPTED, coverage)
    bus.register(FactorHookEvent.CHECKPOINT_COMMITTED, checkpoints)

    clock = FakeClock(NOW)
    loop = make_loop(database, clock, None, hooks=bus)
    checkpoint = await loop.commit_iteration(
        candidates=[{"expr": "mom_20"}],
        factors=[{"name": "f1"}],
        details={"candidates": 5, "accepted": 1},
    )
    await loop.iterate(success=False)
    assert summary.summary()["completed"] == 1
    assert summary.summary()["failed"] == 1
    assert coverage.summary()["total"] == 1
    # 初始建行（iteration=0）、commit_iteration、iterate 各提交一次 checkpoint
    assert len(checkpoints.calls) == 3

    # 崩溃重放场景：同迭代同 digest 的事件再投递不重复计数
    await bus.emit(
        FactorHookEvent.ITERATION_COMPLETED,
        iteration=checkpoint.iteration,
        digest=checkpoint.facts_digest,
        details={"candidates": 5, "accepted": 1},
    )
    assert summary.summary()["completed"] == 1


async def test_loop_emits_profile_stalled_once_across_replays(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    bus = FactorHookBus("factor.discovery", "1.0.0")
    stalled = CountingHook("stalled")
    bus.register(FactorHookEvent.PROFILE_STALLED, stalled)
    clock = FakeClock(NOW)
    loop = make_loop(database, clock, None, hooks=bus, max_consecutive_failures=1)
    checkpoint = await loop.iterate(success=False)
    assert checkpoint.status is FactorLoopStatus.REQUIRES_REVIEW
    # 同键重放（崩溃后重放 stall 事件）不得重复投递
    await bus.emit(
        FactorHookEvent.PROFILE_STALLED,
        iteration=checkpoint.iteration,
        digest=checkpoint.state_digest,
        details={},
    )
    for _ in range(3):
        await loop.initialize()  # 反复重放恢复路径也不重新触发
    assert len(stalled.calls) == 1  # 幂等：stall 事件只投递一次


async def test_search_state_roundtrips_through_pointer(tmp_path: Path) -> None:
    pointer = tmp_path / "checkpoints" / "pointer.json"
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    clock = FakeClock(NOW)
    loop = make_loop(database, clock, pointer)
    assert loop.search_state is None  # 默认无搜索状态
    await loop.commit_iteration(candidates=[{"expr": "mom_5"}], search_state=SEARCH_STATE)
    assert loop.search_state == SEARCH_STATE

    fresh = make_loop(database, FakeClock(NOW), pointer)
    restored = await fresh.initialize()
    assert restored.status is FactorLoopStatus.RUNNING
    assert fresh.search_state == SEARCH_STATE  # 跨 Loop 实例恢复
    await fresh.initialize()  # initialize 重写指针也不得丢状态
    assert fresh.search_state == SEARCH_STATE


async def test_search_state_not_written_when_absent(tmp_path: Path) -> None:
    pointer = tmp_path / "checkpoints" / "pointer.json"
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    loop = make_loop(database, FakeClock(NOW), pointer)
    await loop.commit_iteration()
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert "search_state" not in payload
