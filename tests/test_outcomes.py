from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import (
    AssessmentStatus,
    OutcomeError,
    OutcomeEvaluator,
    OutcomePolicy,
    OutcomeRepository,
    OutcomeRequest,
    TaskStatus,
)
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)
PLAN = "00000000-0000-0000-0000-000000000001"
DECISION = "00000000-0000-0000-0000-000000000002"
GRANT = "00000000-0000-0000-0000-000000000003"
TASK = "00000000-0000-0000-0000-000000000004"
CORRELATION = "00000000-0000-0000-0000-000000000005"
IDS = tuple(UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(20, 80))


def request(**changes: object) -> OutcomeRequest:
    values: dict[str, object] = {
        "task_id": TASK,
        "correlation_id": CORRELATION,
        "task_status": TaskStatus.SUCCEEDED,
        "goal_id": "goal.summary",
        "goal_completed": True,
        "quality_metrics": {"accuracy": 0.9, "clarity": 0.8},
        "baseline_quality": 0.75,
        "cost_ratio": 0.5,
        "risk_violations": (),
        "evidence_ids": ("fact-1", "fact-2"),
        "required_evidence": 2,
    }
    values.update(changes)
    return OutcomeRequest(**values)  # type: ignore[arg-type]


def evaluator(database: SQLiteDatabase, *, ids: tuple[UUID, ...] = IDS) -> OutcomeEvaluator:
    return OutcomeEvaluator(
        database,
        FakeClock(NOW),
        FakeUuidGenerator(ids),
        OutcomePolicy("outcome-rules/1.0", 0.7, 0.05, 1.0),
    )


async def seed_task(database: SQLiteDatabase, *, correlation: str = CORRELATION) -> None:
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO plan VALUES (?, '{}', 'digest', 'CANDIDATE', ?, ?, ?)",
            (PLAN, timestamp, timestamp, correlation),
        )
        await tx.execute(
            "INSERT INTO plan_decision VALUES (?, ?, 'APPROVED', '{}', ?, ?)",
            (DECISION, PLAN, timestamp, correlation),
        )
        await tx.execute(
            "INSERT INTO execution_grant VALUES (?, ?, ?, '{}', 'ACTIVE', ?, ?, ?)",
            (GRANT, DECISION, TASK, timestamp, timestamp, correlation),
        )
        await tx.execute(
            "INSERT INTO task(task_id, grant_id, status, version, attempt, created_at, finished_at, deadline, correlation_id) VALUES (?, ?, 'SUCCEEDED', 1, 1, ?, ?, ?, ?)",
            (TASK, GRANT, timestamp, timestamp, timestamp, correlation),
        )


@pytest.mark.asyncio
async def test_four_assessments_are_independent_and_deterministic(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "outcomes.db")
    await database.initialize()
    result = evaluator(database).evaluate(request())
    assert result.execution.status is AssessmentStatus.PASSED
    assert result.goal.status is AssessmentStatus.PASSED
    assert result.quality.status is AssessmentStatus.PASSED and result.quality.score == 0.85
    assert result.evidence.status is AssessmentStatus.PASSED
    assert result.successful is True

    failed = evaluator(database).evaluate(request(
        task_status=TaskStatus.FAILED,
        goal_completed=False,
        quality_metrics={"accuracy": 0.5},
        baseline_quality=0.8,
        cost_ratio=1.2,
        risk_violations=("unsafe",),
        evidence_ids=("one",),
        required_evidence=2,
    ))
    assert failed.execution.status is AssessmentStatus.FAILED
    assert failed.goal.status is AssessmentStatus.FAILED
    assert failed.quality.status is AssessmentStatus.FAILED
    assert set(failed.quality.reasons) == {
        "quality_below_threshold", "quality_below_baseline", "cost_budget_exceeded", "risk_policy_violated",
    }
    assert failed.evidence.status is AssessmentStatus.PARTIAL and failed.evidence.score == 0.5
    assert failed.successful is False


@pytest.mark.asyncio
async def test_unknown_goal_quality_and_execution_review_are_not_success(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "unknown.db")
    await database.initialize()
    result = evaluator(database).evaluate(request(
        task_status=TaskStatus.REQUIRES_REVIEW,
        goal_completed=None,
        quality_metrics={},
        evidence_ids=(),
        required_evidence=1,
    ))
    assert result.execution.status is AssessmentStatus.UNKNOWN
    assert result.goal.status is AssessmentStatus.UNKNOWN
    assert result.quality.status is AssessmentStatus.UNKNOWN
    assert result.evidence.status is AssessmentStatus.FAILED


@pytest.mark.asyncio
async def test_evaluation_episode_and_event_commit_atomically(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "atomic.db")
    await database.initialize()
    await seed_task(database)
    result = await evaluator(database).evaluate_and_record(request())

    evaluation = await database.fetch_one(
        "SELECT evaluation_json, episode_id FROM outcome_evaluation WHERE evaluation_id = ?",
        (result.evaluation_id,),
    )
    episode = await database.fetch_one("SELECT episode_json FROM episode WHERE episode_id = ?", (result.episode_id,))
    event = await database.fetch_one("SELECT envelope_json FROM outbox_event WHERE correlation_id = ?", (CORRELATION,))
    assert evaluation is not None and str(evaluation["episode_id"]) == result.episode_id
    assert episode is not None and json.loads(str(episode["episode_json"]))["kind"] == "TASK_OUTCOME"
    assert event is not None
    envelope = json.loads(str(event["envelope_json"]))
    assert envelope["msg_type"] == "outcome.evaluated"
    assert envelope["payload"]["data"]["execution"]["status"] == "PASSED"


@pytest.mark.asyncio
async def test_repository_rejects_missing_or_mismatched_task_without_partial_episode(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "reject.db")
    await database.initialize()
    evaluation = evaluator(database).evaluate(request())
    async with database.transaction() as tx:
        with pytest.raises(OutcomeError) as missing:
            await OutcomeRepository(tx).add(evaluation)
        assert missing.value.code == "TASK_NOT_FOUND"
    assert await database.fetch_one("SELECT * FROM episode") is None

    await seed_task(database, correlation="00000000-0000-0000-0000-000000000099")
    async with database.transaction() as tx:
        with pytest.raises(OutcomeError) as mismatch:
            await OutcomeRepository(tx).add(evaluation)
        assert mismatch.value.code == "CORRELATION_MISMATCH"
    assert await database.fetch_one("SELECT * FROM episode") is None


@pytest.mark.asyncio
async def test_repository_rejects_claimed_status_that_differs_from_task(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "status.db")
    await database.initialize()
    await seed_task(database)
    evaluation = evaluator(database).evaluate(request(task_status=TaskStatus.FAILED))
    async with database.transaction() as tx:
        with pytest.raises(OutcomeError) as mismatch:
            await OutcomeRepository(tx).add(evaluation)
        assert mismatch.value.code == "TASK_STATUS_MISMATCH"
    assert await database.fetch_one("SELECT * FROM episode") is None

@pytest.mark.parametrize(
    "kwargs, message",
    (
        ({"task_status": TaskStatus.RUNNING}, "terminal"),
        ({"quality_metrics": {"bad": 1.1}}, "quality metrics"),
        ({"baseline_quality": -0.1}, "baseline"),
        ({"cost_ratio": -1}, "non-negative"),
        ({"evidence_ids": ("same", "same")}, "unique"),
    ),
)
def test_invalid_evaluation_requests_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        request(**kwargs)


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        OutcomePolicy("")
    with pytest.raises(ValueError, match="quality"):
        OutcomePolicy("v1", 2)
    with pytest.raises(ValueError, match="tolerance"):
        OutcomePolicy("v1", baseline_tolerance=2)
    with pytest.raises(ValueError, match="cost"):
        OutcomePolicy("v1", maximum_cost_ratio=-1)
