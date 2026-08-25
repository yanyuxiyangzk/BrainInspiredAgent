from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_loop import FactorDiscoveryLoop, FactorLoopProfile, FactorLoopStatus


@pytest.mark.asyncio
async def test_factor_loop_is_bounded_and_checkpoint_recovers(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    profile = FactorLoopProfile("factor.discovery", "1.0.0", max_iterations=2)
    loop = FactorDiscoveryLoop(database, clock, profile)
    initial = await loop.initialize()
    assert initial.iteration == 0 and initial.status is FactorLoopStatus.RUNNING
    first = await loop.iterate()
    assert first.iteration == 1 and first.status is FactorLoopStatus.RUNNING
    await database.close()

    reopened = SQLiteDatabase(tmp_path / "factor.db")
    await reopened.initialize()
    resumed = FactorDiscoveryLoop(reopened, clock, profile)
    assert (await resumed.initialize()).state_digest == first.state_digest
    done = await resumed.iterate()
    assert done.iteration == 2 and done.status is FactorLoopStatus.COMPLETED
    assert (await resumed.iterate()).iteration == 2
    await reopened.close()


@pytest.mark.asyncio
async def test_factor_loop_pause_resume_terminate_and_failure_review(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = SQLiteDatabase(tmp_path / "factor-state.db")
    await database.initialize()
    loop = FactorDiscoveryLoop(
        database, clock, FactorLoopProfile("factor.discovery", "1.0.0", max_consecutive_failures=2),
    )
    paused = await loop.pause()
    assert paused.status is FactorLoopStatus.PAUSED
    assert (await loop.iterate()).status is FactorLoopStatus.PAUSED
    assert (await loop.resume()).status is FactorLoopStatus.RUNNING
    failed = await loop.iterate(success=False)
    assert failed.consecutive_failures == 1
    review = await loop.iterate(success=False)
    assert review.status is FactorLoopStatus.REQUIRES_REVIEW
    assert (await loop.terminate()).status is FactorLoopStatus.REQUIRES_REVIEW
    await database.close()


@pytest.mark.asyncio
async def test_factor_loop_terminal_controls_are_idempotent(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = SQLiteDatabase(tmp_path / "terminal.db")
    await database.initialize()
    loop = FactorDiscoveryLoop(database, clock, FactorLoopProfile("factor.discovery", "1.0.0", max_iterations=1))
    done = await loop.iterate()
    assert done.status is FactorLoopStatus.COMPLETED
    assert (await loop.pause()).status is FactorLoopStatus.COMPLETED
    assert (await loop.resume()).status is FactorLoopStatus.COMPLETED
    assert (await loop.terminate()).status is FactorLoopStatus.COMPLETED
    await database.close()


def test_factor_loop_profile_rejects_unbounded_configuration() -> None:
    with pytest.raises(ValueError):
        FactorLoopProfile("", "1.0.0")
    with pytest.raises(ValueError):
        FactorLoopProfile("factor.discovery", "1.0.0", max_iterations=0)
    with pytest.raises(ValueError):
        FactorLoopProfile("factor.discovery", "1.0.0", interval=__import__("datetime").timedelta(0))
