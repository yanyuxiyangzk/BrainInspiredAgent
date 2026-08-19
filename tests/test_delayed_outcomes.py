from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import (
    DelayedOutcomeError,
    DelayedOutcomeRepository,
    DelayedOutcomeService,
    OutcomeEvaluator,
    OutcomePolicy,
    OutcomeRequest,
    TaskStatus,
    WindowStatus,
)
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
TASK = "00000000-0000-0000-0000-000000000004"
CORRELATION = "00000000-0000-0000-0000-000000000005"
IDS = tuple(UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(100, 180))


async def seed_task(database: SQLiteDatabase) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, ?)", (stamp, stamp, CORRELATION))
        await tx.execute("INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, ?)", (stamp, CORRELATION))
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', ?, '{}', 'ACTIVE', ?, ?, ?)",
            (TASK, stamp, stamp, CORRELATION),
        )
        await tx.execute(
            "INSERT INTO task(task_id, grant_id, status, version, attempt, created_at, finished_at, deadline, correlation_id) VALUES (?, 'grant', 'SUCCEEDED', 1, 1, ?, ?, ?, ?)",
            (TASK, stamp, stamp, stamp, CORRELATION),
        )


def outcome_request(*, evidence: tuple[str, ...] = ("future-1",)) -> OutcomeRequest:
    return OutcomeRequest(
        TASK, CORRELATION, TaskStatus.SUCCEEDED, "goal.summary", True,
        {"future_quality": 0.9}, 0.8, 0.5, (), evidence, 1,
    )


@pytest.mark.asyncio
async def test_window_ledger_due_order_and_pinned_version(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "delayed.db")
    await database.initialize()
    await seed_task(database)
    clock = FakeClock(NOW)
    service = DelayedOutcomeService(database, clock, FakeUuidGenerator(IDS))
    window = await service.open_window(
        task_id=TASK, episode_id=None, opens_at=NOW, closes_at=NOW + timedelta(minutes=5),
        evaluator_version="future/1.0", correlation_id=CORRELATION,
    )
    assert window.status is WindowStatus.OPEN and window.evaluator_version == "future/1.0"
    clock.advance(60)
    entry = await service.append(
        window.window_id, evidence_id="future-1", evidence_type="OBSERVATION",
        evidence={"score": 0.9}, observed_at=NOW + timedelta(minutes=1), correlation_id=CORRELATION,
    )
    assert entry.digest.startswith("sha256:")
    with pytest.raises(DelayedOutcomeError) as duplicate:
        await service.append(
            window.window_id, evidence_id="future-1", evidence_type="OBSERVATION",
            evidence={"score": 0.1}, observed_at=NOW + timedelta(minutes=1), correlation_id=CORRELATION,
        )
    assert duplicate.value.code == "EVIDENCE_ALREADY_EXISTS"

    async with database.transaction() as tx:
        repository = DelayedOutcomeRepository(tx)
        assert await repository.due_windows(now=NOW + timedelta(minutes=4)) == ()
        assert (await repository.evidence(window.window_id))[0].evidence == {"score": 0.9}
    clock.advance(240)
    async with database.transaction() as tx:
        due = await DelayedOutcomeRepository(tx).due_windows(now=clock.now())
        assert tuple(item.window_id for item in due) == (window.window_id,)
    closed = await service.close(window.window_id)
    assert closed.status is WindowStatus.CLOSED

    evaluator = OutcomeEvaluator(
        database, clock, FakeUuidGenerator(IDS[20:]), OutcomePolicy("future/1.0")
    )
    evaluation = await service.evaluate(window.window_id, evaluator=evaluator, request=outcome_request())
    assert evaluation.evaluator_version == "future/1.0"
    stored = await database.fetch_one("SELECT status FROM delayed_evaluation_window WHERE window_id = ?", (window.window_id,))
    assert stored is not None and stored["status"] == WindowStatus.EVALUATED
    assert await database.fetch_one("SELECT * FROM outcome_evaluation WHERE evaluation_id = ?", (evaluation.evaluation_id,))


@pytest.mark.asyncio
async def test_window_rejects_early_late_and_post_close_evidence(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "boundaries.db")
    await database.initialize()
    await seed_task(database)
    clock = FakeClock(NOW)
    service = DelayedOutcomeService(database, clock, FakeUuidGenerator(IDS))
    window = await service.open_window(
        task_id=TASK, episode_id=None, opens_at=NOW + timedelta(seconds=10),
        closes_at=NOW + timedelta(seconds=20), evaluator_version="v1", correlation_id=CORRELATION,
    )
    with pytest.raises(DelayedOutcomeError) as early:
        await service.append(
            window.window_id, evidence_id="early", evidence_type="OBSERVATION", evidence={},
            observed_at=NOW, correlation_id=CORRELATION,
        )
    assert early.value.code == "WINDOW_NOT_OPEN"
    with pytest.raises(DelayedOutcomeError) as not_due:
        await service.close(window.window_id)
    assert not_due.value.code == "WINDOW_NOT_DUE"
    clock.advance(20)
    with pytest.raises(DelayedOutcomeError) as late:
        await service.append(
            window.window_id, evidence_id="late", evidence_type="OBSERVATION", evidence={},
            observed_at=clock.now(), correlation_id=CORRELATION,
        )
    assert late.value.code == "WINDOW_EXPIRED"
    await service.close(window.window_id)
    with pytest.raises(DelayedOutcomeError) as closed:
        await service.append(
            window.window_id, evidence_id="closed", evidence_type="OBSERVATION", evidence={},
            observed_at=clock.now(), correlation_id=CORRELATION,
        )
    assert closed.value.code == "WINDOW_CLOSED"


@pytest.mark.asyncio
async def test_delayed_evaluation_requires_closed_window_version_and_exact_ledger(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "validation.db")
    await database.initialize()
    await seed_task(database)
    clock = FakeClock(NOW)
    service = DelayedOutcomeService(database, clock, FakeUuidGenerator(IDS))
    window = await service.open_window(
        task_id=TASK, episode_id=None, opens_at=NOW, closes_at=NOW + timedelta(seconds=1),
        evaluator_version="v1", correlation_id=CORRELATION,
    )
    await service.append(
        window.window_id, evidence_id="future-1", evidence_type="RESULT", evidence={"value": 1},
        observed_at=NOW, correlation_id=CORRELATION,
    )
    v1 = OutcomeEvaluator(database, clock, FakeUuidGenerator(IDS[20:]), OutcomePolicy("v1"))
    with pytest.raises(DelayedOutcomeError) as open_error:
        await service.evaluate(window.window_id, evaluator=v1, request=outcome_request())
    assert open_error.value.code == "WINDOW_NOT_CLOSED"
    clock.advance(1)
    await service.close(window.window_id)
    wrong_version = OutcomeEvaluator(database, clock, FakeUuidGenerator(IDS[30:]), OutcomePolicy("v2"))
    with pytest.raises(DelayedOutcomeError) as version:
        await service.evaluate(window.window_id, evaluator=wrong_version, request=outcome_request())
    assert version.value.code == "EVALUATOR_VERSION_MISMATCH"
    with pytest.raises(DelayedOutcomeError) as ledger:
        await service.evaluate(window.window_id, evaluator=v1, request=outcome_request(evidence=("other",)))
    assert ledger.value.code == "EVIDENCE_LEDGER_MISMATCH"


@pytest.mark.asyncio
async def test_unknown_window_and_invalid_time_are_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid.db")
    await database.initialize()
    await seed_task(database)
    clock = FakeClock(NOW)
    service = DelayedOutcomeService(database, clock, FakeUuidGenerator(IDS))
    with pytest.raises(DelayedOutcomeError) as missing:
        await service.close("missing")
    assert missing.value.code == "WINDOW_NOT_FOUND"
    with pytest.raises(DelayedOutcomeError) as invalid:
        await service.open_window(
            task_id=TASK, episode_id=None, opens_at=NOW, closes_at=NOW,
            evaluator_version="v1", correlation_id=CORRELATION,
        )
    assert invalid.value.code == "WINDOW_INVALID"
