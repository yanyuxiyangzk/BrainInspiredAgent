from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import RepairError, RepairOutcome, RestRepair
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowValidator
from apps.quant_agent import DAILY_REVIEW_WORKFLOW

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
DAY = date(2026, 8, 18)
CORRELATION = "00000000-0000-0000-0000-000000000005"
TASK = "00000000-0000-0000-0000-000000000004"
IDS = tuple(UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(300, 380))


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


async def add_episode(database: SQLiteDatabase, episode_id: str, successful: bool | None) -> None:
    document: dict[str, object] = {"kind": "TASK_OUTCOME"}
    if successful is not None:
        document["evaluation"] = {"successful": successful}
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO episode VALUES (?, ?, ?, ?, ?)",
            (episode_id, TASK, json.dumps(document), NOW.isoformat().replace("+00:00", "Z"), CORRELATION),
        )


@pytest.mark.asyncio
async def test_daily_review_aggregates_episodes_and_is_restart_idempotent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "review.db")
    await database.initialize()
    await seed_task(database)
    await add_episode(database, "episode-success", True)
    await add_episode(database, "episode-failed", False)
    await add_episode(database, "episode-unknown", None)
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(IDS))

    decision = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert decision.outcome is RepairOutcome.REQUESTED
    assert decision.summary is not None
    assert (decision.summary.total, decision.summary.successful, decision.summary.failed, decision.summary.unknown) == (3, 1, 1, 1)
    assert decision.summary.episode_ids == ("episode-failed", "episode-success", "episode-unknown")
    assert decision.request is not None and decision.request.requires_model is True
    assert decision.request.review_key == "daily_review:2026-08-18"

    restarted = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(IDS[10:]))
    duplicate = await restarted.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert duplicate.outcome is RepairOutcome.DUPLICATE
    rows = await database.fetch_all("SELECT * FROM rest_repair_run")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_no_activity_review_uses_deterministic_no_model_path(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "empty.db")
    await database.initialize()
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    decision = await repair.prepare(DAY, mode="REVIEW", phase="HOLIDAY")
    assert decision.summary is not None and decision.summary.classification == "NO_ACTIVITY"
    assert decision.request is not None and decision.request.requires_model is False
    assert decision.request.parameters["summary"] == decision.summary.to_dict()


@pytest.mark.asyncio
async def test_review_requires_eligible_state_and_retries_failed_run(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "retry.db")
    await database.initialize()
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(IDS), max_attempts=2)
    ineligible = await repair.prepare(DAY, mode="NORMAL", phase="CLOSED")
    assert ineligible.outcome is RepairOutcome.INELIGIBLE
    first = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert first.request is not None
    await repair.fail(first.request.run_id, error_code="STORE_UNAVAILABLE")
    retry = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert retry.request is not None and retry.request.attempt == 2
    await repair.fail(retry.request.run_id, error_code="STORE_UNAVAILABLE")
    exhausted = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert exhausted.outcome is RepairOutcome.DUPLICATE


@pytest.mark.asyncio
async def test_completion_only_accepts_evidenced_candidate_experience(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "complete.db")
    await database.initialize()
    await seed_task(database)
    await add_episode(database, "episode-1", True)
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    prepared = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert prepared.request is not None
    with pytest.raises(RepairError) as promoted:
        await repair.complete(
            prepared.request.run_id, result={},
            candidate_experiences=({"status": "VALIDATED", "evidence_episode_ids": ["episode-1"]},),
        )
    assert promoted.value.code == "EXPERIENCE_STATE_INVALID"
    with pytest.raises(RepairError) as evidence:
        await repair.complete(
            prepared.request.run_id, result={},
            candidate_experiences=({"status": "CANDIDATE", "evidence_episode_ids": ["other"]},),
        )
    assert evidence.value.code == "EXPERIENCE_EVIDENCE_INVALID"
    await repair.complete(
        prepared.request.run_id,
        result={"summary": "done"},
        candidate_experiences=({"status": "CANDIDATE", "evidence_episode_ids": ["episode-1"]},),
    )
    row = await database.fetch_one("SELECT status, result_json FROM rest_repair_run")
    assert row is not None and row["status"] == "SUCCEEDED"
    assert json.loads(str(row["result_json"]))["candidate_experiences"][0]["status"] == "CANDIDATE"
    with pytest.raises(RepairError) as inactive:
        await repair.complete(prepared.request.run_id, result={})
    assert inactive.value.code == "REPAIR_NOT_ACTIVE"


def test_daily_review_workflow_is_valid_application_definition() -> None:
    validation = WorkflowValidator().validate(DAILY_REVIEW_WORKFLOW)
    assert validation.workflow_id == "daily_review"
    assert validation.topological_order == ("summarize",)


def test_invalid_repair_configuration_is_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "config.db")
    clock = FakeClock(NOW)
    with pytest.raises(ValueError, match="workflow"):
        RestRepair(database, clock, FakeUuidGenerator(IDS), workflow_id="")
    with pytest.raises(ValueError, match="positive"):
        RestRepair(database, clock, FakeUuidGenerator(IDS), deadline_seconds=0)
