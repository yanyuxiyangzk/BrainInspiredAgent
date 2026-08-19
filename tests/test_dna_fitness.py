from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import Assessment, AssessmentStatus, OutcomeEvaluation, TaskStatus
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import (
    DnaDefinition,
    DnaFitnessError,
    DnaFitnessObservation,
    DnaFitnessPolicy,
    DnaFitnessProjector,
    FitnessReadiness,
    PersistentDnaRegistry,
)

NOW = datetime(2026, 8, 18, 8, tzinfo=UTC)


def workflow() -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "fitness_flow", "version": "1.0.0",
        "name": "Fitness flow", "input_schema": {"type": "object"},
        "policy": {"timeout_seconds": 10, "max_parallelism": 1,
                   "required_capabilities": ["market.summarize"]},
        "nodes": [{"node_id": "summary", "type": "skill", "depends_on": [],
                   "capability": "market.summarize", "capability_version": "1.0",
                   "input": {}, "constraints": {"side_effect": "PURE"}}],
        "output_mapping": {"summary": "$.nodes.summary.output"},
    }


def assessment(status: AssessmentStatus, score: float | None) -> Assessment:
    return Assessment(status, score, ("test",))


def outcome(index: int, *, successful: bool = True, quality: float | None = 0.8,
            evidence: float | None = 1.0, correlation: str | None = None) -> OutcomeEvaluation:
    identity = f"00000000-0000-0000-0000-{index:012d}"
    passed = AssessmentStatus.PASSED if successful else AssessmentStatus.FAILED
    return OutcomeEvaluation(
        f"evaluation-{index}", f"episode-{index}", identity,
        correlation or f"correlation-{index}", NOW + timedelta(minutes=index), "rules/1",
        TaskStatus.SUCCEEDED if successful else TaskStatus.FAILED, f"goal-{index}",
        (f"evidence-{index}",), assessment(passed, 1.0 if successful else 0.0),
        assessment(passed, 1.0 if successful else 0.0),
        assessment(AssessmentStatus.UNKNOWN if quality is None else passed, quality),
        assessment(AssessmentStatus.UNKNOWN if evidence is None else passed, evidence),
    )


async def persist_outcome(
    database: SQLiteDatabase, definition: DnaDefinition, value: OutcomeEvaluation,
) -> None:
    suffix = value.task_id[-12:]
    timestamp = value.evaluated_at.isoformat().replace("+00:00", "Z")
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO plan VALUES (?, '{}', ?, 'CANDIDATE', ?, ?, ?)",
            (f"plan-{suffix}", f"digest-{suffix}", timestamp, timestamp, value.correlation_id),
        )
        await transaction.execute(
            "INSERT INTO plan_decision VALUES (?, ?, 'APPROVED', '{}', ?, ?)",
            (f"decision-{suffix}", f"plan-{suffix}", timestamp, value.correlation_id),
        )
        await transaction.execute(
            "INSERT INTO execution_grant VALUES (?, ?, ?, '{}', 'ACTIVE', ?, ?, ?)",
            (f"grant-{suffix}", f"decision-{suffix}", value.task_id,
             timestamp, timestamp, value.correlation_id),
        )
        await transaction.execute(
            """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                 finished_at,deadline,correlation_id)
               VALUES (?,?,?,1,1,?,?,?,?)""",
            (value.task_id, f"grant-{suffix}", value.task_status.value,
             timestamp, timestamp, timestamp, value.correlation_id),
        )
        await transaction.execute(
            """INSERT INTO workflow_run(
                   run_id,task_id,workflow_id,workflow_version,workflow_digest,input_digest,
                   status,deadline,created_at,correlation_id
               ) VALUES (?,?,?,?,?,?,'SUCCEEDED',?,?,?)""",
            (f"run-{suffix}", value.task_id, definition.workflow_validation.workflow_id,
             definition.version, definition.workflow_validation.digest, f"input-{suffix}",
             timestamp, timestamp, value.correlation_id),
        )
        await transaction.execute(
            "INSERT INTO episode VALUES (?,?,?,?,?)",
            (value.episode_id, value.task_id, "{}", timestamp, value.correlation_id),
        )
        await transaction.execute(
            "INSERT INTO outcome_evaluation VALUES (?,?,?,?,?,?)",
            (value.evaluation_id, value.task_id, value.episode_id,
             json.dumps(value.to_dict()), timestamp, value.correlation_id),
        )


async def setup(tmp_path: Path, *, minimum: int = 2, risk: float = 0.0) -> tuple[
    SQLiteDatabase, FakeClock, DnaDefinition, DnaFitnessProjector,
]:
    database = SQLiteDatabase(tmp_path / "fitness.db")
    await database.initialize()
    clock = FakeClock(NOW)
    definition = DnaDefinition.from_workflow(workflow())
    await PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(1, 20))
    ).register(definition, correlation_id="dna")
    projector = DnaFitnessProjector(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(100, 200)),
        DnaFitnessPolicy("fitness/1", "window-2026-08-18", NOW,
                         NOW + timedelta(hours=1), minimum, risk),
    )
    return database, clock, definition, projector


def observation(
    definition: DnaDefinition, value: OutcomeEvaluation, *, cost: int = 10,
    latency: int = 100, stable: bool = True, risks: tuple[str, ...] = (),
) -> DnaFitnessObservation:
    return DnaFitnessObservation(
        definition.dna_id, definition.version, definition.content_digest, value,
        cost, latency, stable, risks,
    )


@pytest.mark.asyncio
async def test_projects_multi_dimensional_vector_and_closes_window(tmp_path: Path) -> None:
    database, clock, definition, projector = await setup(tmp_path)
    first = outcome(1)
    second = outcome(2, successful=False, quality=0.4, evidence=0.5)
    await persist_outcome(database, definition, first)
    await persist_outcome(database, definition, second)
    collecting = await projector.project(observation(definition, first, cost=10, latency=100))
    assert collecting.readiness is FitnessReadiness.COLLECTING
    projected = await projector.project(
        observation(definition, second, cost=30, latency=300, stable=False)
    )
    assert projected.sample_count == 2 and projected.revision == 2
    assert projected.success_rate == 0.5
    assert 0 < projected.success_confidence_lower < projected.success_rate
    assert projected.evidence_score == 0.75
    assert projected.user_value_score == 0.6
    assert projected.average_cost_minor == 20
    assert projected.average_latency_ms == 200 and projected.p95_latency_ms == 300
    assert projected.stability_rate == 0.5 and projected.risk_rate == 0
    assert projected.readiness is FitnessReadiness.OBSERVING
    clock.advance(3601)
    ready = await projector.refresh(definition.dna_id, definition.version)
    assert ready.readiness is FitnessReadiness.READY and ready.revision == 3
    assert await projector.get(definition.dna_id, definition.version) == ready
    assert len(await projector.observations(definition.dna_id, definition.version)) == 2
    await database.close()


@pytest.mark.asyncio
async def test_risk_dimension_blocks_readiness_instead_of_being_averaged_away(
    tmp_path: Path,
) -> None:
    database, clock, definition, projector = await setup(tmp_path, minimum=1, risk=0.0)
    value = outcome(1)
    await persist_outcome(database, definition, value)
    snapshot = await projector.project(
        observation(definition, value, risks=("permission_escalation",))
    )
    assert snapshot.success_rate == 1 and snapshot.user_value_score == 0.8
    assert snapshot.risk_rate == 1 and snapshot.readiness is FitnessReadiness.RISK_BLOCKED
    clock.advance(3601)
    assert (await projector.refresh(definition.dna_id, definition.version)).readiness \
        is FitnessReadiness.RISK_BLOCKED
    await database.close()


@pytest.mark.asyncio
async def test_projection_is_idempotent_and_conflicting_attribution_is_rejected(
    tmp_path: Path,
) -> None:
    database, _, definition, projector = await setup(tmp_path, minimum=1)
    value = outcome(1)
    await persist_outcome(database, definition, value)
    item = observation(definition, value)
    first = await projector.project(item)
    duplicate = await projector.project(item)
    assert duplicate == first
    with pytest.raises(DnaFitnessError, match="different DNA attribution"):
        await projector.project(observation(definition, value, cost=11))
    assert len(await projector.observations(definition.dna_id, definition.version)) == 1
    rows = await projector.observations(definition.dna_id, definition.version)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_fitness_observation WHERE observation_id=?",
                (rows[0]["observation_id"],),
            )
    await database.close()


@pytest.mark.asyncio
async def test_projection_rejects_unverifiable_identity_outcome_and_window(tmp_path: Path) -> None:
    database, _, definition, projector = await setup(tmp_path)
    value = outcome(1)
    with pytest.raises(DnaFitnessError, match="not persisted"):
        await projector.project(observation(definition, value))
    await persist_outcome(database, definition, value)
    wrong_digest = DnaFitnessObservation(
        definition.dna_id, definition.version, "sha256:wrong", value, 1, 1, True
    )
    with pytest.raises(DnaFitnessError, match="digest does not match"):
        await projector.project(wrong_digest)
    missing = DnaFitnessObservation("missing.dna", "1.0.0", definition.content_digest,
                                    value, 1, 1, True)
    with pytest.raises(DnaFitnessError, match="not registered"):
        await projector.project(missing)
    forged = replace(value, correlation_id="forged-correlation")
    with pytest.raises(DnaFitnessError, match="correlation does not match"):
        await projector.project(observation(definition, forged))
    async with database.transaction() as transaction:
        await transaction.execute("DELETE FROM workflow_run WHERE task_id=?", (value.task_id,))
    with pytest.raises(DnaFitnessError, match="no matching DNA workflow run"):
        await projector.project(observation(definition, value))
    outside = outcome(100)
    with pytest.raises(DnaFitnessError, match="outside"):
        await projector.project(observation(definition, outside))
    with pytest.raises(DnaFitnessError, match="not found"):
        await projector.get(definition.dna_id, definition.version)
    await database.close()


def test_fitness_contracts_reject_invalid_values() -> None:
    with pytest.raises(DnaFitnessError, match="identifiers"):
        DnaFitnessPolicy("", "window", NOW, NOW + timedelta(days=1))
    with pytest.raises(DnaFitnessError, match="positive duration"):
        DnaFitnessPolicy("v1", "window", NOW, NOW)
    with pytest.raises(DnaFitnessError, match="minimum_samples"):
        DnaFitnessPolicy("v1", "window", NOW, NOW + timedelta(days=1), 0)
    with pytest.raises(DnaFitnessError, match="risk and confidence"):
        DnaFitnessPolicy("v1", "window", NOW, NOW + timedelta(days=1),
                         maximum_risk_rate=2)
    value = outcome(1)
    with pytest.raises(DnaFitnessError, match="identity"):
        DnaFitnessObservation("", "1", "bad", value, 0, 0, True)
    with pytest.raises(DnaFitnessError, match="non-negative"):
        DnaFitnessObservation("dna", "1", "sha256:x", value, -1, 0, True)
    with pytest.raises(DnaFitnessError, match="unique"):
        DnaFitnessObservation("dna", "1", "sha256:x", value, 0, 0, True, ("x", "x"))
    with pytest.raises(DnaFitnessError, match="timezone-aware"):
        DnaFitnessPolicy("v1", "window", NOW.replace(tzinfo=None), NOW + timedelta(days=1))
