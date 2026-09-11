"""Post-install smoke: build a wheel-installed loop, crash-recover it, verify digests."""
import asyncio
import datetime
import pathlib
import tempfile

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_loop import (
    FactorDiscoveryLoop,
    FactorLoopProfile,
    FactorLoopStatus,
)

NOW = datetime.datetime(2026, 9, 5, tzinfo=datetime.UTC)


async def smoke() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = SQLiteDatabase(tmp / "smoke.db")
    await db.initialize()
    pointer = tmp / "checkpoints" / "pointer.json"
    loop = FactorDiscoveryLoop(
        db, FakeClock(NOW), FactorLoopProfile("smoke", "1.0.0"), checkpoint_path=pointer
    )
    await loop.commit_iteration(candidates=[{"expr": "mom_20"}], factors=[{"name": "f1"}])
    assert pointer.is_file(), "atomic pointer missing"

    stale = pointer.read_text()  # 模拟：事实已提交、指针未更新即崩溃
    second = await loop.commit_iteration(candidates=[{"expr": "rev_5"}])
    pointer.write_text(stale)

    fresh_db = SQLiteDatabase(tmp / "smoke.db")
    await fresh_db.initialize()
    fresh = FactorDiscoveryLoop(
        fresh_db, FakeClock(NOW), FactorLoopProfile("smoke", "1.0.0"), checkpoint_path=pointer
    )
    recovered = await fresh.initialize()
    assert recovered.iteration == second.iteration, "recovery lost a committed iteration"
    assert recovered.status is FactorLoopStatus.RUNNING
    assert recovered.facts_digest == second.facts_digest
    assert await fresh.filter_untested([{"expr": "mom_20"}, {"expr": "rev_5"}, {"expr": "new"}]) == [
        {"expr": "new"}
    ], "tested candidates were re-offered"
    print(
        "WSL packaging smoke PASS: iteration",
        recovered.iteration,
        "facts_digest",
        recovered.facts_digest[:20],
    )
    await db.close()
    await fresh_db.close()


asyncio.run(smoke())
