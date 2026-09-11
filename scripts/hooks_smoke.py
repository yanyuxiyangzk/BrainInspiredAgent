"""Post-install smoke for L-009: hook idempotency, summaries, search-state resume."""
import asyncio
import datetime
import pathlib
import tempfile

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_hooks import (
    FactorCoverageCollector,
    FactorHookBus,
    FactorHookEvent,
    FactorIterationSummaryCollector,
)
from domain_sdk.factor_loop import (
    FactorDiscoveryLoop,
    FactorLoopProfile,
    FactorLoopStatus,
)

NOW = datetime.datetime(2026, 9, 7, tzinfo=datetime.UTC)
SEARCH_STATE = {
    "format": 1,
    "rounds": 4,
    "momentum": 0.3,
    "exploration_share": 0.4,
    "window_step": 1,
    "zero_accept_streak": 0,
    "group_weights": {"mutate": 0.5},
}


async def smoke() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    database = SQLiteDatabase(tmp / "smoke.db")
    await database.initialize()

    bus = FactorHookBus("factor.discovery", "1.0.0")
    summary = FactorIterationSummaryCollector()
    coverage = FactorCoverageCollector()
    bus.register(FactorHookEvent.ITERATION_COMPLETED, summary)
    bus.register(FactorHookEvent.ITERATION_FAILED, summary)
    bus.register(FactorHookEvent.FACTOR_ACCEPTED, coverage)

    pointer = tmp / "checkpoints" / "pointer.json"
    clock = FakeClock(NOW)
    loop = FactorDiscoveryLoop(
        database, clock, FactorLoopProfile("factor.discovery", "1.0.0"),
        checkpoint_path=pointer, hooks=bus,
    )
    await loop.commit_iteration(
        candidates=[{"expr": "mom_20"}],
        factors=[{"name": "f1"}],
        details={"candidates": 8, "accepted": 1, "strategy": "mutate"},
        search_state=SEARCH_STATE,
    )
    await loop.iterate(success=False)
    assert summary.summary()["completed"] == 1
    assert summary.summary()["failed"] == 1
    assert coverage.summary()["total"] == 1

    fresh = FactorDiscoveryLoop(
        database, FakeClock(NOW), FactorLoopProfile("factor.discovery", "1.0.0"),
        checkpoint_path=pointer,
    )
    checkpoint = await fresh.initialize()
    assert checkpoint.status is FactorLoopStatus.RUNNING
    assert fresh.search_state == SEARCH_STATE, "search state must survive restart via pointer"

    print(
        "WSL packaging smoke PASS: delivered",
        summary.summary()["iterations"],
        "iteration events; coverage",
        coverage.summary()["total"],
        "factor; search_state rounds",
        (fresh.search_state or {}).get("rounds"),
    )


asyncio.run(smoke())
