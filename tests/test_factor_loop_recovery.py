"""L-002 tests: atomic checkpoint pointer, tested-candidate hashes, factor library digest and recovery consistency."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_loop import (
    FactorDiscoveryLoop,
    FactorLoopProfile,
    FactorLoopStatus,
    compute_facts_digest,
    compute_library_digest,
)

PROFILE_ID = "factor.discovery"
VERSION = "1.0.0"


def make_database(tmp_path: Path, name: str = "factor.db") -> SQLiteDatabase:
    return SQLiteDatabase(tmp_path / name)


def make_profile(**overrides: Any) -> FactorLoopProfile:
    values: dict[str, Any] = {"max_consecutive_failures": 3}
    values.update(overrides)
    return FactorLoopProfile(PROFILE_ID, VERSION, **values)


def make_loop(
    database: SQLiteDatabase, clock: FakeClock, pointer: Path | None, **profile_overrides: Any
) -> FactorDiscoveryLoop:
    return FactorDiscoveryLoop(
        database, clock, make_profile(**profile_overrides), checkpoint_path=pointer
    )


async def checkpoint_row(database: SQLiteDatabase) -> Any:
    return await database.fetch_one(
        "SELECT * FROM discovery_loop_checkpoint WHERE profile_id=? AND version=?",
        (PROFILE_ID, VERSION),
    )


async def facts_digest_from_rows(database: SQLiteDatabase) -> str:
    candidates = await database.fetch_all(
        "SELECT candidate_hash, algorithm_version FROM factor_candidate"
    )
    factors = await database.fetch_all("SELECT factor_hash FROM factor_library")
    return compute_facts_digest(
        [(str(row["candidate_hash"]), str(row["algorithm_version"])) for row in candidates],
        [str(row["factor_hash"]) for row in factors],
    )


@pytest.mark.asyncio
async def test_crash_between_fact_commit_and_pointer_write_resumes_without_rework(
    tmp_path: Path,
) -> None:
    """中断续跑：SQLite 事实已提交而文件指针落后时，恢复到事实状态且不重复处理候选。"""
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "checkpoints" / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    stale_pointer = pointer.read_text()
    assert json.loads(stale_pointer)["iteration"] == 0

    committed = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    assert committed.iteration == 1
    pointer.write_text(stale_pointer)  # 模拟：事实提交后、指针落盘前崩溃
    await database.close()

    reopened = make_database(tmp_path)
    await reopened.initialize()
    resumed = make_loop(reopened, clock, pointer)
    recovered = await resumed.initialize()
    assert recovered.iteration == committed.iteration
    assert recovered.status is FactorLoopStatus.RUNNING
    assert recovered.facts_digest == committed.facts_digest

    rewritten = json.loads(pointer.read_text())
    assert rewritten["iteration"] == committed.iteration
    assert rewritten["facts_digest"] == committed.facts_digest

    untested = await resumed.filter_untested([{"expr": "mom_20"}, {"expr": "rev_5"}])
    assert untested == [{"expr": "rev_5"}]
    await reopened.close()


@pytest.mark.asyncio
async def test_pointer_ahead_of_database_enters_review_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    committed = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])

    forged = json.loads(pointer.read_text())
    forged["iteration"] = committed.iteration + 5
    pointer.write_text(json.dumps(forged))

    reviewed = await loop.initialize()
    assert reviewed.status is FactorLoopStatus.REQUIRES_REVIEW
    row = await checkpoint_row(database)
    assert row["iteration"] == committed.iteration  # 迭代计数未被指针篡改覆盖
    assert row["status"] == "REQUIRES_REVIEW"
    assert json.loads(pointer.read_text())["iteration"] == committed.iteration + 5  # 证据保留
    assert (await loop.iterate()).status is FactorLoopStatus.REQUIRES_REVIEW
    await database.close()


@pytest.mark.asyncio
async def test_corrupt_pointer_enters_review_without_overwrite(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    committed = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])

    pointer.write_text("{corrupt")
    assert (await loop.initialize()).status is FactorLoopStatus.REQUIRES_REVIEW
    pointer.write_text("[]")  # 合法 JSON 但不是对象，同样视为损坏
    assert (await loop.initialize()).status is FactorLoopStatus.REQUIRES_REVIEW
    row = await checkpoint_row(database)
    assert row["iteration"] == committed.iteration
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"iteration": 99},
        {"state_digest": "sha256:forged"},
        {"facts_digest": "sha256:forged"},
        {"format": 0},
        {"profile_id": "other"},
        {"version": "9.9.9"},
        {"iteration": "many"},
        {"iteration": True},
        {"iteration": -1},
    ],
)
async def test_forged_pointer_variants_enter_review(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    forged = json.loads(pointer.read_text())
    forged.update(mutation)
    pointer.write_text(json.dumps(forged))
    assert (await loop.initialize()).status is FactorLoopStatus.REQUIRES_REVIEW
    await database.close()


@pytest.mark.asyncio
async def test_unreadable_pointer_is_treated_as_corrupt(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    pointer.unlink()
    pointer.mkdir()  # 指针位置是目录 → OSError → 视为损坏
    assert (await loop.initialize()).status is FactorLoopStatus.REQUIRES_REVIEW
    await database.close()


@pytest.mark.asyncio
async def test_reconcile_is_noop_outside_review(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    created = await loop.reconcile()  # 空库时等价于 initialize
    assert created.iteration == 0
    assert created.status is FactorLoopStatus.RUNNING
    assert (await loop.reconcile()) == created  # 非审查态不产生任何变更
    await database.close()


@pytest.mark.asyncio
async def test_terminate_from_running_and_iterate_past_max(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None, max_iterations=5)
    await loop.initialize()
    terminated = await loop.terminate()
    assert terminated.status is FactorLoopStatus.TERMINATED
    await database.close()


@pytest.mark.asyncio
async def test_iterate_at_max_marks_completed_without_new_iteration(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None, max_iterations=1)
    done = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    assert done.status is FactorLoopStatus.COMPLETED  # commit_iteration 达到上限即 COMPLETED
    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE discovery_loop_checkpoint SET status='RUNNING' WHERE profile_id=?",
            (PROFILE_ID,),
        )
    advanced = await loop.iterate()
    assert advanced.iteration == done.iteration  # RUNNING 但已达上限：不再新增迭代
    assert advanced.status is FactorLoopStatus.COMPLETED
    await database.close()



@pytest.mark.asyncio
async def test_missing_pointer_self_heals_from_database(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    committed = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    pointer.unlink()

    recovered = await loop.initialize()
    assert recovered.status is FactorLoopStatus.RUNNING
    assert json.loads(pointer.read_text())["iteration"] == committed.iteration
    await database.close()


@pytest.mark.asyncio
async def test_fact_tampering_is_detected_and_reconcile_restains_running(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    await loop.commit_iteration(
        candidates=[{"expr": "mom_20"}],
        factors=[{"name": "f1"}, {"name": "f2"}],
    )
    async with database.transaction() as tx:
        await tx.execute(
            "DELETE FROM factor_library WHERE factor_json = ?", (json.dumps({"name": "f1"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False),)
        )

    reviewed = await loop.initialize()
    assert reviewed.status is FactorLoopStatus.REQUIRES_REVIEW
    remaining = await database.fetch_all("SELECT * FROM factor_library")
    assert len(remaining) == 1  # 恢复不修补也不扩大事实损伤（禁止盲目覆盖）

    reconciled = await loop.reconcile()
    assert reconciled.status is FactorLoopStatus.RUNNING
    assert (await loop.initialize()).status is FactorLoopStatus.RUNNING
    advanced = await loop.iterate()
    assert advanced.iteration == reviewed.iteration + 1
    await database.close()


@pytest.mark.asyncio
async def test_legacy_checkpoint_without_facts_digest_self_heals(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    async with database.transaction() as tx:
        await tx.execute("UPDATE discovery_loop_checkpoint SET facts_digest=NULL")

    healed = await loop.initialize()
    assert healed.status is FactorLoopStatus.RUNNING
    row = await checkpoint_row(database)
    assert row["facts_digest"] == await facts_digest_from_rows(database)
    await database.close()


@pytest.mark.asyncio
async def test_commit_refuses_when_not_running_and_writes_no_facts(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    await loop.initialize()

    paused = await loop.pause()
    assert paused.status is FactorLoopStatus.PAUSED
    refused = await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    assert refused.status is FactorLoopStatus.PAUSED
    assert refused.iteration == paused.iteration
    assert await loop.tested_hashes() == frozenset()
    await database.close()


@pytest.mark.asyncio
async def test_commit_persists_digests_matching_recomputed_facts(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    await loop.initialize()

    first = await loop.commit_iteration(
        candidates=[{"expr": "mom_20"}], factors=[{"name": "f1"}]
    )
    assert first.facts_digest == await facts_digest_from_rows(database)
    row = await database.fetch_one("SELECT library_digest FROM factor_library")
    hashes = [str(item["factor_hash"]) for item in await database.fetch_all("SELECT factor_hash FROM factor_library")]
    assert str(row["library_digest"]) == compute_library_digest(hashes)

    duplicate = await loop.commit_iteration(factors=[{"name": "f1"}])
    assert duplicate.facts_digest == first.facts_digest  # 重复因子不改变库摘要

    grown = await loop.commit_iteration(factors=[{"name": "f2"}])
    assert grown.facts_digest != first.facts_digest
    assert grown.facts_digest == await facts_digest_from_rows(database)
    await database.close()


@pytest.mark.asyncio
async def test_filter_untested_isolates_algorithm_versions(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    loop = make_loop(database, clock, None)
    await loop.initialize()
    await loop.commit_iteration(candidates=[{"expr": "mom_20"}], algorithm_version="1")

    assert await loop.tested_hashes() >= {FactorDiscoveryLoop.candidate_hash({"expr": "mom_20"}, algorithm_version="1")}
    assert FactorDiscoveryLoop.candidate_hash({"expr": "mom_20"}, algorithm_version="2") not in await loop.tested_hashes()
    remaining = await loop.filter_untested(
        [{"expr": "mom_20"}, {"expr": "mom_10"}], algorithm_version="2"
    )
    assert remaining == [{"expr": "mom_20"}, {"expr": "mom_10"}]
    await database.close()


def test_candidate_hash_is_stable_and_algorithm_versioned() -> None:
    base = FactorDiscoveryLoop.candidate_hash({"a": 1, "b": [1, 2]})
    assert base == FactorDiscoveryLoop.candidate_hash({"b": [1, 2], "a": 1})
    assert base != FactorDiscoveryLoop.candidate_hash({"a": 1, "b": [1, 2]}, algorithm_version="2")
    assert base != FactorDiscoveryLoop.candidate_hash({"a": 2, "b": [1, 2]})


@pytest.mark.asyncio
async def test_pointer_write_is_atomic_and_complete(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "deep" / "nested" / "pointer.json"
    loop = make_loop(database, clock, pointer)
    created = await loop.initialize()

    payload = json.loads(pointer.read_text())
    assert payload["iteration"] == created.iteration
    assert payload["state_digest"] == created.state_digest
    assert payload["facts_digest"] == created.facts_digest
    assert payload["format"] == 1
    assert list(pointer.parent.glob(".checkpoint-*")) == []  # 无临时文件残留
    await database.close()


@pytest.mark.asyncio
async def test_review_pointer_recovers_when_pointer_file_removed(tmp_path: Path) -> None:
    """指针被人工移除且无伪造证据时，事实一致即可恢复 RUNNING。"""
    clock = FakeClock(datetime(2026, 8, 25, tzinfo=UTC))
    database = make_database(tmp_path)
    await database.initialize()
    pointer = tmp_path / "pointer.json"
    loop = make_loop(database, clock, pointer)
    await loop.initialize()
    await loop.commit_iteration(candidates=[{"expr": "mom_20"}])
    pointer.unlink()
    assert (await loop.initialize()).status is FactorLoopStatus.RUNNING
    await database.close()
