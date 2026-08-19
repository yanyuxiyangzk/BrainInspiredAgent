from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.motor import MotorExec
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.rest_repair import RepairOutcome, RestRepair
from active_agent_platform.risk import RiskBudget, RiskGate, RiskPolicy
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from active_agent_platform.state import BrainMode, BrainState, MarketPhase, Workload
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runtime import WorkflowRuntime
from apps.quant_agent import (
    DAILY_REVIEW_WORKFLOW,
    SUMMARY_CAPABILITY,
    DailyReviewApp,
    install_fake_skills,
)

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
DAY = date(2026, 8, 18)
SOURCE_TASK = "00000000-0000-0000-0000-000000000004"
SOURCE_CORRELATION = "00000000-0000-0000-0000-000000000005"


class Logger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields


async def _episodes(database: SQLiteDatabase) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO plan VALUES ('source-plan', '{}', 'source-digest', 'CANDIDATE', ?, ?, ?)",
            (stamp, stamp, SOURCE_CORRELATION),
        )
        await transaction.execute(
            "INSERT INTO plan_decision VALUES ('source-decision', 'source-plan', 'APPROVED', '{}', ?, ?)",
            (stamp, SOURCE_CORRELATION),
        )
        await transaction.execute(
            "INSERT INTO execution_grant VALUES ('source-grant', 'source-decision', ?, '{}', 'ACTIVE', ?, ?, ?)",
            (SOURCE_TASK, stamp, stamp, SOURCE_CORRELATION),
        )
        await transaction.execute(
            """INSERT INTO task(task_id, grant_id, status, created_at, finished_at,
                                 deadline, correlation_id)
               VALUES (?, 'source-grant', 'SUCCEEDED', ?, ?, ?, ?)""",
            (SOURCE_TASK, stamp, stamp, stamp, SOURCE_CORRELATION),
        )
        for episode_id, successful in (("episode-success", True), ("episode-failed", False)):
            document = json.dumps(
                {"kind": "TASK_OUTCOME", "evaluation": {"successful": successful}}
            )
            await transaction.execute(
                "INSERT INTO episode VALUES (?, ?, ?, ?, ?)",
                (episode_id, SOURCE_TASK, document, stamp, SOURCE_CORRELATION),
            )


@pytest.mark.asyncio
async def test_daily_review_executes_once_and_survives_restart(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    identifiers = FakeUuidGenerator(UUID(int=index) for index in range(1000, 1500))
    database = SQLiteDatabase(tmp_path / "daily.db")
    await database.initialize()
    await _episodes(database)
    registry = WorkflowRegistry()
    registered = registry.register(DAILY_REVIEW_WORKFLOW, status=WorkflowStatus.VALIDATED)
    workflow = registry.activate(registered.workflow_id, registered.version)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    binding = SkillResolver(capabilities, skills, clock=clock).resolve(
        SkillRequirement("summarize", SUMMARY_CAPABILITY, "1.0", frozenset(), SideEffect.PURE),
        policy_version="daily-review/1",
    )
    bindings = {(workflow.workflow_id, workflow.version, "summarize"): binding}
    artifacts = LocalArtifactStore(tmp_path / "objects")
    runtime = WorkflowRuntime(
        database=database,
        registry=registry,
        skill_invoker=SkillInvoker(skills, bundle.adapters),
        skill_context=SkillContext(
            clock, Logger(), CancellationToken(), artifacts, {}, ResourceBudget(10)
        ),
        artifacts=artifacts,
        clock=clock,
        identifiers=identifiers,
    )
    risk = RiskGate(RiskPolicy(
        "daily-review/1", frozenset({SUMMARY_CAPABILITY}), frozenset(),
        RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
    ))
    state = BrainState(MarketPhase.CLOSED, Workload.IDLE, BrainMode.REVIEW, NOW)
    app = DailyReviewApp(
        database, RestRepair(database, clock, identifiers), PlanValidator(registry), risk,
        MotorExec(database, runtime, clock=clock, identifiers=identifiers), clock, identifiers,
    )
    result = await app.execute(DAY, state, bindings)
    assert result.decision.outcome is RepairOutcome.REQUESTED
    assert result.execution is not None and result.execution.status.value == "SUCCEEDED"
    assert result.execution.output["review"]["item_count"] == 2  # type: ignore[index]
    assert len(result.candidate_experiences) == 1
    candidate = result.candidate_experiences[0]
    assert candidate["status"] == "CANDIDATE"
    assert set(cast(list[str], candidate["evidence_episode_ids"])) == {
        "episode-success", "episode-failed"
    }
    assert bundle.summary.invocation_count == 1
    row = await database.fetch_one("SELECT status, attempt, result_json FROM rest_repair_run")
    assert row is not None and (row["status"], row["attempt"]) == ("SUCCEEDED", 1)
    assert json.loads(str(row["result_json"]))["candidate_experiences"][0]["status"] == "CANDIDATE"

    restarted = DailyReviewApp(
        database, RestRepair(database, clock, identifiers), PlanValidator(registry), risk,
        MotorExec(database, runtime, clock=clock, identifiers=identifiers), clock, identifiers,
    )
    duplicate = await restarted.execute(DAY, state, bindings)
    assert duplicate.decision.outcome is RepairOutcome.DUPLICATE
    assert duplicate.execution is None and bundle.summary.invocation_count == 1
    counts = await database.fetch_one(
        "SELECT (SELECT count(*) FROM rest_repair_run), (SELECT count(*) FROM workflow_run)"
    )
    assert counts is not None and tuple(counts) == (1, 1)

    # Six virtual hours later the process-facing database remains responsive.
    clock.advance(timedelta(hours=6).total_seconds())
    assert await database.fetch_one("SELECT 1 AS alive") is not None
    await database.close()


@pytest.mark.asyncio
async def test_no_activity_daily_review_avoids_candidate_experience(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "empty.db")
    await database.initialize()
    decision = await RestRepair(
        database, FakeClock(NOW), FakeUuidGenerator((UUID(int=1), UUID(int=2)))
    ).prepare(DAY, mode="REVIEW", phase="HOLIDAY")
    assert decision.summary is not None and decision.summary.classification == "NO_ACTIVITY"
    assert decision.request is not None and decision.request.requires_model is False
    await database.close()
