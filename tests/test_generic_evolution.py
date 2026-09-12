"""G-01 tests: domain-neutral evolution auto-plan on the generic branch.

`apps/generic_evolution.auto_plan_candidate` 不绑定任何领域：从持久化 fitness
快照检测弱点（EvolutionDriver 规则策略），生成受治理操作并经
DnaCandidateGenerator 落候选提案；`brainagent evolution auto-plan` 为 CLI 入口。
夹具用中性样例工作流（tests/sample_domain）与直接播种的事实行。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sample_domain import SAMPLE_WORKFLOW

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from apps.generic_evolution import auto_plan_candidate
from domain_sdk import DnaDefinition, DnaParent, DnaStatus, PersistentDnaRegistry
from domain_sdk.experience_dataset import ExperienceDatasetBuilder, ExperienceDatasetSpec

NOW = datetime(2026, 9, 12, 8, tzinfo=UTC)
BASE_ID = "workflow.sample_summary"
WINDOW = "fitness-window-2026-09-12"


def _variant_workflow() -> dict[str, object]:
    document = json.loads(json.dumps(SAMPLE_WORKFLOW))
    for node in document["nodes"]:  # type: ignore[index]
        if node.get("node_id") == "build_summary":  # type: ignore[union-attr]
            node["input"]["title"] = "Shadow variant"  # type: ignore[index]
    document["version"] = "1.0.1"
    return document


async def _seed(
    database: SQLiteDatabase, *, user_value: float = 0.4, readiness: str = "READY",
    drop_snapshot: bool = False,
) -> tuple[str, str]:
    """Register ACTIVE baseline + candidate variant, seed facts, build dataset.

    Returns (baseline_dna_id, dataset_id).
    """
    clock = FakeClock(NOW)
    registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=i) for i in range(1, 50))
    )
    baseline_record = await registry.register(
        DnaDefinition.from_workflow(SAMPLE_WORKFLOW, dna_id=BASE_ID, version="1.0.0"),
        correlation_id="test:seed:baseline",
    )
    for status in (DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY):
        baseline_record = await registry.transition(
            BASE_ID, baseline_record.dna.version, status,
            expected_revision=baseline_record.revision,
            reason="prepare active baseline", correlation_id="test:seed:baseline",
        )
    baseline_record = await registry.activate(
        BASE_ID, baseline_record.dna.version,
        expected_revision=baseline_record.revision,
        reason="prepare active baseline", correlation_id="test:seed:baseline",
    )
    baseline = baseline_record.dna
    variant_record = await registry.register(
        DnaDefinition.from_workflow(
            _variant_workflow(), dna_id=BASE_ID, version="1.0.1",
            parent_dna=(DnaParent(BASE_ID, baseline.version, baseline.content_digest),),
        ),
        correlation_id="test:seed:variant",
    )
    variant = variant_record.dna

    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        for digest, tag in (
            (baseline.content_digest, "base"), (variant.content_digest, "cand"),
        ):
            correlation = f"correlation-{tag}"
            evaluation = {
                "evaluation_id": f"evaluation-{tag}", "episode_id": f"episode-{tag}",
                "task_id": f"task-{tag}", "correlation_id": correlation,
                "evaluated_at": stamp, "evaluator_version": "rules/1",
                "task_status": "SUCCEEDED", "goal_id": f"goal-{tag}",
                "evidence_ids": [f"evidence-{tag}"],
                "execution": {"status": "PASSED", "score": 1.0, "reasons": ["test"]},
                "goal": {"status": "PASSED", "score": 1.0, "reasons": ["test"]},
                "quality": {"status": "PASSED", "score": 0.8, "reasons": ["test"]},
                "evidence": {"status": "PASSED", "score": 1.0, "reasons": ["test"]},
            }
            await tx.execute(
                "INSERT INTO plan VALUES (?,?,?,?,?,?,?)",
                (f"plan-{tag}", json.dumps({"plan": tag}), f"digest-{tag}",
                 "CANDIDATE", stamp, stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO plan_decision VALUES (?,?,?,?,?,?)",
                (f"decision-{tag}", f"plan-{tag}", "APPROVED",
                 json.dumps({"decision": "APPROVED"}), stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO execution_grant VALUES (?,?,?,?,?,?,?,?)",
                (f"grant-{tag}", f"decision-{tag}", f"task-{tag}",
                 json.dumps({"grant": tag}), "ACTIVE", stamp, stamp, correlation),
            )
            await tx.execute(
                """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                     finished_at,deadline,correlation_id)
                   VALUES (?,?,'SUCCEEDED',1,1,?,?,?,?)""",
                (f"task-{tag}", f"grant-{tag}", stamp, stamp, stamp, correlation),
            )
            await tx.execute(
                """INSERT INTO workflow_run(
                       run_id,task_id,workflow_id,workflow_version,workflow_digest,
                       input_digest,status,deadline,created_at,correlation_id
                   ) VALUES (?,?,?,?,?,?,'SUCCEEDED',?,?,?)""",
                (f"run-{tag}", f"task-{tag}", "sample_summary", baseline.version,
                 baseline.workflow_validation.digest, f"input-{tag}", stamp, stamp,
                 correlation),
            )
            await tx.execute(
                "INSERT INTO episode VALUES (?,?,?,?,?)",
                (f"episode-{tag}", f"task-{tag}",
                 json.dumps({"evidence": [f"evidence-{tag}"]}), stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO outcome_evaluation VALUES (?,?,?,?,?,?)",
                (f"evaluation-{tag}", f"task-{tag}", f"episode-{tag}",
                 json.dumps(evaluation), stamp, correlation),
            )
            await tx.execute(
                """INSERT INTO dna_fitness_observation VALUES (
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (
                    f"observation-{tag}", f"evaluation-{tag}", f"task-{tag}",
                    BASE_ID, baseline.version, digest, WINDOW,
                    1, 1.0, 0.8, 10, 100, 1, "[]", stamp,
                    f"sha256:observation-{tag}", correlation,
                ),
            )
        if not drop_snapshot:
            await tx.execute(
                """INSERT INTO dna_fitness_snapshot VALUES (
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (
                    BASE_ID, baseline.version, baseline.content_digest, WINDOW,
                    "fitness/1", 2, 1.0, 0.95, 1.0, user_value, 10.0, 100.0, 100,
                    1.0, 0.0, readiness, stamp, 1,
                ),
            )

    dataset = await ExperienceDatasetBuilder(database, clock).build(
        ExperienceDatasetSpec(
            dataset_id="generic-replay-ds", version="1.0.0",
            builder_version="generic-evolution/1.0", window_id=WINDOW,
            starts_at=NOW - timedelta(minutes=5),
            train_until=NOW + timedelta(minutes=5),
            validation_until=NOW + timedelta(minutes=10),
            ends_at=NOW + timedelta(minutes=15),
            baseline_content_digest=baseline.content_digest,
            candidate_content_digests=(variant.content_digest,),
        )
    )
    del dataset
    return BASE_ID, "generic-replay-ds"


@pytest.mark.asyncio
async def test_auto_plan_proposes_governed_candidate(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "auto.db")
    await database.initialize()
    dna_id, dataset_id = await _seed(database, user_value=0.4)
    try:
        result = await auto_plan_candidate(
            database, proposal_id="gen-prop-1", baseline_dna_id=dna_id,
            dataset_id=dataset_id, dataset_version="1.0.0",
        )
        assert result.status == "PROPOSED"
        assert result.weakness == "user_value_score"
        assert result.source == "rule"
        assert result.proposal_id == "gen-prop-1"
        assert result.candidate_version == "1.0.1"
        row = await database.fetch_one(
            "SELECT operations_json FROM dna_candidate_proposal WHERE proposal_id=?",
            ("gen-prop-1",),
        )
        assert row is not None
        operations = json.loads(str(row["operations_json"]))
        assert operations[0]["kind"] == "SET_INPUT"
        assert operations[0]["node_id"] == "build_summary"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_auto_plan_reports_no_weakness(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "healthy.db")
    await database.initialize()
    dna_id, dataset_id = await _seed(database, user_value=1.0)
    try:
        result = await auto_plan_candidate(
            database, proposal_id="gen-prop-2", baseline_dna_id=dna_id,
            dataset_id=dataset_id, dataset_version="1.0.0",
        )
        assert result.status == "NO_WEAKNESS"
        count = await database.fetch_one(
            "SELECT count(*) AS total FROM dna_candidate_proposal"
        )
        assert count is not None and int(count["total"]) == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_auto_plan_reports_risk_blocked(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "risk.db")
    await database.initialize()
    dna_id, dataset_id = await _seed(database, readiness="RISK_BLOCKED")
    try:
        result = await auto_plan_candidate(
            database, proposal_id="gen-prop-3", baseline_dna_id=dna_id,
            dataset_id=dataset_id, dataset_version="1.0.0",
        )
        assert result.status == "RISK_BLOCKED"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_auto_plan_rejects_missing_snapshot_or_baseline(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "nosnap.db")
    await database.initialize()
    dna_id, dataset_id = await _seed(database, drop_snapshot=True)
    try:
        result = await auto_plan_candidate(
            database, proposal_id="gen-prop-4", baseline_dna_id=dna_id,
            dataset_id=dataset_id, dataset_version="1.0.0",
        )
        assert result.status == "REJECTED"
        assert result.reason and "snapshot" in result.reason

        missing = await auto_plan_candidate(
            database, proposal_id="gen-prop-5", baseline_dna_id="workflow.absent",
            dataset_id=dataset_id, dataset_version="1.0.0",
        )
        assert missing.status == "REJECTED"
        assert "ACTIVE" in (missing.reason or "")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cli_auto_plan_roundtrip(tmp_path: Path) -> None:
    from apps.brainagent_cli import run

    database_path = tmp_path / "cli.db"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    dna_id, dataset_id = await _seed(database, user_value=0.4)
    del dna_id
    await database.close()

    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = await run((
            "--database", str(database_path), "evolution", "auto-plan", "cli-prop-1",
            "--baseline", BASE_ID, "--dataset-id", dataset_id,
            "--dataset-version", "1.0.0",
        ))
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "PROPOSED"
    assert payload["weakness"] == "user_value_score"

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = await run((
            "--database", str(database_path), "evolution", "auto-plan",
        ))
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "REJECTED"  # 缺 proposal ID / dataset 参数
