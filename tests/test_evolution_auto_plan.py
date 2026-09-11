"""自动进化装配测试：/evolution auto-plan —— fitness 弱点 → 受治理候选提案。

EvolutionDriver（E05）此前只有库级测试；本文件验证应用层接线：从持久化的
fitness 快照自动检测弱点、生成受治理操作并落一个候选提案（E03），无需人工
提供 operations/hypothesis。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from active_agent_platform.foundation import SystemClock, Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.auto_evolution import auto_plan_candidate
from apps.quant_agent.candidate_service import bump_version
from apps.quant_agent.dataset_service import build_experience_dataset
from apps.quant_agent.evolution_seed import (
    DEFAULT_START,
    seed_baseline,
    seed_market_days,
)
from domain_sdk.dna import DnaDefinition, DnaParent, DnaStatus
from domain_sdk.dna_repository import PersistentDnaRegistry

WEAKEN_SQL = (
    "UPDATE dna_fitness_snapshot SET user_value_score=0.4 WHERE dna_id=?"
)


async def _prepare(
    database: SQLiteDatabase, tmp_path: Path, *, dataset_id: str = "auto-plan-ds",
) -> str:
    """播种 ACTIVE 基线、影子候选变体与数据集，返回基线 dna_id。"""
    report = await seed_baseline(
        database, start=DEFAULT_START, days=4, artifacts_dir=tmp_path / "art-base",
    )
    base_row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? AND version=?",
        (report.dna_id, report.version),
    )
    assert base_row is not None
    base = DnaDefinition.from_document(
        cast("dict[str, object]", json.loads(str(base_row["document_json"])))
    )
    workflow = cast("dict[str, object]", json.loads(json.dumps(base.to_document()["workflow"])))
    for node in cast("list[dict[str, object]]", workflow["nodes"]):
        if node.get("node_id") == "build_summary":
            cast("dict[str, object]", node["input"])["title"] = "Shadow summary"
    workflow["version"] = bump_version(base.version)
    candidate = DnaDefinition.from_workflow(
        workflow, dna_id=base.dna_id, version=bump_version(base.version),
        status=DnaStatus.CANDIDATE,
        parent_dna=(DnaParent(base.dna_id, base.version, base.content_digest),),
    )
    registry = PersistentDnaRegistry(database, clock := SystemClock(), Uuid7Generator(clock))
    await registry.register(candidate, correlation_id="test:auto-plan:candidate")
    await seed_market_days(
        database, workflow_document=workflow, dna_id=candidate.dna_id,
        version=candidate.version, content_digest=candidate.content_digest,
        start=DEFAULT_START, days=4, artifacts_dir=tmp_path / "art-cand",
        window_id=f"seed-{DEFAULT_START:%Y%m%d}-4", start_offset_seconds=2.0,
    )
    await build_experience_dataset(
        database, dataset_id=dataset_id, window_id=report.window_id,
        baseline_content_digest=report.content_digest,
        candidate_content_digests=(candidate.content_digest,),
        starts_at=DEFAULT_START, ends_at=DEFAULT_START + timedelta(days=5),
    )
    return report.dna_id


@pytest.mark.asyncio
async def test_auto_plan_proposes_governed_candidate_from_weakness(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "auto.db")
    await database.initialize()
    dna_id = await _prepare(database, tmp_path)
    async with database.transaction() as tx:
        await tx.execute(WEAKEN_SQL, (dna_id,))

    result = await auto_plan_candidate(
        database, proposal_id="prop-auto-1", dataset_id="auto-plan-ds",
        dataset_version="1.0.0",
    )
    assert result.status == "PROPOSED"
    assert result.weakness == "user_value_score"
    assert result.source == "rule"
    assert result.proposal_id == "prop-auto-1"
    assert result.hypothesis and "user_value_score" in result.hypothesis
    row = await database.fetch_one(
        "SELECT operations_json FROM dna_candidate_proposal WHERE proposal_id=?",
        ("prop-auto-1",),
    )
    assert row is not None
    operations = json.loads(str(row["operations_json"]))
    assert operations[0]["kind"] == "SET_INPUT"


@pytest.mark.asyncio
async def test_auto_plan_reports_no_weakness_on_healthy_fitness(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "healthy.db")
    await database.initialize()
    dna_id = await _prepare(database, tmp_path)
    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE dna_fitness_snapshot SET success_rate=1.0, evidence_score=1.0, "
            "user_value_score=1.0, stability_rate=1.0 WHERE dna_id=?",
            (dna_id,),
        )
    result = await auto_plan_candidate(
        database, proposal_id="prop-auto-2", dataset_id="auto-plan-ds",
        dataset_version="1.0.0",
    )
    assert result.status == "NO_WEAKNESS"
    remaining = await database.fetch_one(
        "SELECT count(*) AS total FROM dna_candidate_proposal"
    )
    assert remaining is not None and int(remaining["total"]) == 0


@pytest.mark.asyncio
async def test_auto_plan_reports_risk_blocked(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "risk.db")
    await database.initialize()
    dna_id = await _prepare(database, tmp_path)
    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE dna_fitness_snapshot SET readiness='RISK_BLOCKED' WHERE dna_id=?",
            (dna_id,),
        )
    result = await auto_plan_candidate(
        database, proposal_id="prop-auto-3", dataset_id="auto-plan-ds",
        dataset_version="1.0.0",
    )
    assert result.status == "RISK_BLOCKED"


@pytest.mark.asyncio
async def test_auto_plan_requires_fitness_snapshot(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "nosnap.db")
    await database.initialize()
    await _prepare(database, tmp_path)
    async with database.transaction() as tx:
        await tx.execute("DELETE FROM dna_fitness_snapshot")
    result = await auto_plan_candidate(
        database, proposal_id="prop-auto-4", dataset_id="auto-plan-ds",
        dataset_version="1.0.0",
    )
    assert result.status == "REJECTED"
    assert result.reason and "snapshot" in result.reason


@pytest.mark.asyncio
async def test_cli_auto_plan_roundtrip(tmp_path: Path) -> None:
    from io import StringIO

    from apps.quant_agent.cli import run as run_cli

    database_path = tmp_path / "cli.db"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    dna_id = await _prepare(database, tmp_path)
    async with database.transaction() as tx:
        await tx.execute(WEAKEN_SQL, (dna_id,))
    await database.close()

    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(database_path), "evolution", "auto-plan", "prop-cli-auto",
         "--dataset-id", "auto-plan-ds", "--dataset-version", "1.0.0"),
        stdout, stderr,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "PROPOSED"
    assert payload["weakness"] == "user_value_score"

    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(database_path), "evolution", "auto-plan"),
        stdout, stderr,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "REJECTED"  # 缺 proposal ID / dataset 参数
