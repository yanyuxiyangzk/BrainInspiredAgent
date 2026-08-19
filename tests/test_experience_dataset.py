from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import (
    DatasetCohort,
    DatasetSplit,
    DnaDefinition,
    ExperienceDatasetBuilder,
    ExperienceDatasetError,
    ExperienceDatasetSpec,
    PersistentDnaRegistry,
)

START = datetime(2026, 8, 18, 8, tzinfo=UTC)


def workflow(version: str) -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "dataset_flow", "version": version,
        "name": "Dataset flow", "input_schema": {"type": "object"},
        "policy": {"timeout_seconds": 10, "max_parallelism": 1,
                   "required_capabilities": ["market.summarize"]},
        "nodes": [{"node_id": "summary", "type": "skill", "depends_on": [],
                   "capability": "market.summarize", "capability_version": "1.0",
                   "input": {}, "constraints": {"side_effect": "PURE"}}],
        "output_mapping": {"summary": "$.nodes.summary.output"},
    }


async def seed_sample(
    database: SQLiteDatabase, definition: DnaDefinition, index: int, observed_at: datetime,
) -> None:
    suffix = f"{index:04d}"
    stamp = observed_at.isoformat().replace("+00:00", "Z")
    correlation = f"correlation-{suffix}"
    evaluation = {
        "evaluation_id": f"evaluation-{suffix}", "episode_id": f"episode-{suffix}",
        "task_id": f"task-{suffix}", "correlation_id": correlation,
        "evaluated_at": stamp, "evidence_ids": [f"evidence-{suffix}"],
        "successful": True, "quality": {"score": 0.8}, "evidence": {"score": 1.0},
    }
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO plan VALUES (?,?,?,?,?,?,?)",
            (f"plan-{suffix}", json.dumps({"plan": suffix}), f"digest-{suffix}",
             "CANDIDATE", stamp, stamp, correlation),
        )
        await transaction.execute(
            "INSERT INTO plan_decision VALUES (?,?,?,?,?,?)",
            (f"decision-{suffix}", f"plan-{suffix}", "APPROVED",
             json.dumps({"decision": "APPROVED"}), stamp, correlation),
        )
        await transaction.execute(
            "INSERT INTO execution_grant VALUES (?,?,?,?,?,?,?,?)",
            (f"grant-{suffix}", f"decision-{suffix}", f"task-{suffix}",
             json.dumps({"grant": suffix}), "ACTIVE", stamp, stamp, correlation),
        )
        await transaction.execute(
            """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                 finished_at,deadline,correlation_id)
               VALUES (?,?,'SUCCEEDED',1,1,?,?,?,?)""",
            (f"task-{suffix}", f"grant-{suffix}", stamp, stamp, stamp, correlation),
        )
        await transaction.execute(
            """INSERT INTO workflow_run(
                   run_id,task_id,workflow_id,workflow_version,workflow_digest,input_digest,
                   status,deadline,created_at,correlation_id
               ) VALUES (?,?,?,?,?,?,'SUCCEEDED',?,?,?)""",
            (f"run-{suffix}", f"task-{suffix}", definition.workflow_validation.workflow_id,
             definition.version, definition.workflow_validation.digest, f"input-{suffix}",
             stamp, stamp, correlation),
        )
        await transaction.execute(
            "INSERT INTO episode VALUES (?,?,?,?,?)",
            (f"episode-{suffix}", f"task-{suffix}",
             json.dumps({"evidence": [f"evidence-{suffix}"]}), stamp, correlation),
        )
        await transaction.execute(
            "INSERT INTO outcome_evaluation VALUES (?,?,?,?,?,?)",
            (f"evaluation-{suffix}", f"task-{suffix}", f"episode-{suffix}",
             json.dumps(evaluation), stamp, correlation),
        )
        await transaction.execute(
            "INSERT INTO dna_fitness_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"observation-{suffix}", f"evaluation-{suffix}", f"task-{suffix}",
             definition.dna_id, definition.version, definition.content_digest, "fitness-window",
             1, 1.0, 0.8, 10 + index, 100 + index, 1, "[]", stamp,
             f"sha256:observation-{suffix}", correlation),
        )


async def setup(tmp_path: Path) -> tuple[
    SQLiteDatabase, DnaDefinition, DnaDefinition, ExperienceDatasetBuilder,
]:
    database = SQLiteDatabase(tmp_path / "dataset.db")
    await database.initialize()
    baseline = DnaDefinition.from_workflow(workflow("1.0.0"))
    candidate = DnaDefinition.from_workflow(workflow("2.0.0"))
    registry = PersistentDnaRegistry(
        database, FakeClock(START),
        FakeUuidGenerator(UUID(int=value) for value in range(1, 50)),
    )
    await registry.register(baseline, correlation_id="baseline")
    await registry.register(candidate, correlation_id="candidate")
    moments = (START + timedelta(minutes=10), START + timedelta(minutes=20),
               START + timedelta(hours=1, minutes=10),
               START + timedelta(hours=1, minutes=20),
               START + timedelta(hours=2, minutes=10),
               START + timedelta(hours=2, minutes=20))
    for index, moment in enumerate(moments, 1):
        await seed_sample(database, baseline if index % 2 else candidate, index, moment)
    return database, baseline, candidate, ExperienceDatasetBuilder(
        database, FakeClock(START + timedelta(hours=4))
    )


def spec(baseline: DnaDefinition, candidate: DnaDefinition, **changes: object) -> ExperienceDatasetSpec:
    values: dict[str, object] = {
        "dataset_id": "market.experience", "version": "1.0.0",
        "builder_version": "experience-builder/1.0", "window_id": "fitness-window",
        "starts_at": START, "train_until": START + timedelta(hours=1),
        "validation_until": START + timedelta(hours=2), "ends_at": START + timedelta(hours=3),
        "baseline_content_digest": baseline.content_digest,
        "candidate_content_digests": (candidate.content_digest,),
        "minimum_samples_per_dna": 3,
    }
    values.update(changes)
    return ExperienceDatasetSpec(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_builds_sealed_temporal_dataset_with_attribution_and_trace(tmp_path: Path) -> None:
    database, baseline, candidate, builder = await setup(tmp_path)
    await seed_sample(database, baseline, 90, START + timedelta(hours=3, minutes=1))
    dataset = await builder.build(spec(baseline, candidate))
    assert dataset.manifest.sample_count == 6
    assert (dataset.manifest.train_count, dataset.manifest.validation_count,
            dataset.manifest.test_count) == (2, 2, 2)
    assert [item.split for item in dataset.samples] == [
        DatasetSplit.TRAIN, DatasetSplit.TRAIN, DatasetSplit.VALIDATION,
        DatasetSplit.VALIDATION, DatasetSplit.TEST, DatasetSplit.TEST,
    ]
    assert [item.cohort for item in dataset.samples].count(DatasetCohort.BASELINE) == 3
    first = dataset.samples[0].document
    assert first["sources"]["evaluation_id"] == "evaluation-0001"  # type: ignore[index]
    assert first["episode"] == {"evidence": ["evidence-0001"]}
    assert first["trace"]["workflow_runs"][0]["workflow_digest"] \
        == baseline.workflow_validation.digest  # type: ignore[index]
    assert (await builder.replay("market.experience", "1.0.0")) == dataset
    await database.close()


@pytest.mark.asyncio
async def test_dataset_version_is_idempotent_and_cannot_absorb_later_facts(tmp_path: Path) -> None:
    database, baseline, candidate, builder = await setup(tmp_path)
    definition = spec(baseline, candidate)
    first = await builder.build(definition)
    await seed_sample(database, baseline, 99, START + timedelta(minutes=30))
    assert await builder.build(definition) == first
    with pytest.raises(ExperienceDatasetError, match="another spec"):
        await builder.build(spec(baseline, candidate, minimum_samples_per_dna=2))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_experience_dataset WHERE dataset_id='market.experience'"
            )
    await database.close()


@pytest.mark.asyncio
async def test_replay_detects_sample_and_manifest_tampering(tmp_path: Path) -> None:
    database, baseline, candidate, builder = await setup(tmp_path)
    await builder.build(spec(baseline, candidate))
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_experience_sample_no_update")
        await transaction.execute(
            """UPDATE dna_experience_sample SET document_json='{}'
               WHERE dataset_id='market.experience' AND ordinal=0"""
        )
    with pytest.raises(ExperienceDatasetError, match="sample digest mismatch"):
        await builder.replay("market.experience", "1.0.0")
    await database.close()


@pytest.mark.asyncio
async def test_builder_rejects_incomplete_cohorts_and_missing_dataset(tmp_path: Path) -> None:
    database, baseline, candidate, builder = await setup(tmp_path)
    with pytest.raises(ExperienceDatasetError, match="insufficient samples"):
        await builder.build(spec(baseline, candidate, minimum_samples_per_dna=4))
    with pytest.raises(ExperienceDatasetError, match="not found"):
        await builder.get("missing.dataset", "1.0.0")
    await database.close()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dataset_id": "X"}, "dataset_id"),
        ({"version": "one"}, "version"),
        ({"builder_version": ""}, "builder and window"),
        ({"train_until": START}, "boundaries"),
        ({"baseline_content_digest": "bad"}, "baseline"),
        ({"candidate_content_digests": ()}, "candidate"),
        ({"candidate_content_digests": ("sha256:base", "sha256:base")}, "candidate"),
        ({"minimum_samples_per_dna": 0}, "minimum_samples"),
        ({"starts_at": START.replace(tzinfo=None)}, "timezone-aware"),
    ],
)
def test_dataset_spec_rejects_invalid_contracts(changes: dict[str, object], message: str) -> None:
    baseline = DnaDefinition.from_workflow(workflow("1.0.0"))
    candidate = DnaDefinition.from_workflow(workflow("2.0.0"))
    with pytest.raises(ExperienceDatasetError, match=message):
        spec(baseline, candidate, **changes)
