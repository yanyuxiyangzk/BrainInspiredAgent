"""Sandbox executor tests: determinism, success path and fault injection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent import MARKET_SUMMARY_WORKFLOW
from apps.quant_agent.sandbox import quant_sandbox_executor
from domain_sdk.dna import DnaDefinition, DnaStatus
from domain_sdk.dna_replay import FaultScenario, ReplayContext
from domain_sdk.dna_sandbox_executor import SandboxPolicy
from domain_sdk.experience_dataset import DatasetCohort, DatasetSplit, ExperienceSample

NOW = datetime(2026, 1, 5, 1, 25, tzinfo=UTC)


def _sample(sample_id: str, trade_date: str) -> ExperienceSample:
    document = {"parameters": {
        "symbols": ["INDEX.TEST"], "trade_date": trade_date, "title": "Sandbox",
    }}
    digest = "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True).encode(),
    ).hexdigest()
    return ExperienceSample(
        sample_id=sample_id, ordinal=1, split=DatasetSplit.VALIDATION,
        cohort=DatasetCohort.BASELINE, dna_id="workflow.market_summary",
        dna_version="1.0.0", content_digest="sha256:" + "0" * 64,
        evaluation_id=f"eval-{sample_id}", observation_id=f"obs-{sample_id}",
        observed_at=NOW, sample_digest=digest, document=document,
    )


def _context(replay_id: str, sample: ExperienceSample, fault: FaultScenario) -> ReplayContext:
    return ReplayContext(replay_id, sample.sample_id, NOW, "seed", fault)


@pytest.mark.asyncio
async def test_sandbox_executes_market_summary_successfully_and_deterministically(
    tmp_path: Path,
) -> None:
    del tmp_path
    dna = DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
        status=DnaStatus.ACTIVE,
    )
    executor = quant_sandbox_executor()
    sample = _sample("sample-ok", "2026-01-05")
    context = _context("replay-ok", sample, FaultScenario.NONE)

    first = await executor.execute(dna, sample, context)
    second = await executor.execute(dna, sample, context)

    assert first == second, "identical inputs must yield identical measurements"
    assert first.successful is True
    assert first.stable is True
    assert first.evidence_score == 1.0 and first.user_value_score == 1.0
    assert first.cost_minor == 3  # read market → generate summary → notify
    assert first.risk_violations == ()
    assert first.output_digest.startswith("sha256:")


@pytest.mark.asyncio
async def test_sandbox_skill_failure_fault_fails_the_run(tmp_path: Path) -> None:
    del tmp_path
    dna = DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
    )
    executor = quant_sandbox_executor()
    sample = _sample("sample-fail", "2026-01-06")
    context = _context("replay-fail", sample, FaultScenario.SKILL_FAILURE)

    first = await executor.execute(dna, sample, context)
    second = await executor.execute(dna, sample, context)

    assert first == second
    assert first.successful is False
    assert first.stable is False
    assert first.evidence_score == 0.0


@pytest.mark.asyncio
async def test_sandbox_corrupt_output_is_measured_deterministically(tmp_path: Path) -> None:
    del tmp_path
    dna = DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
    )
    executor = quant_sandbox_executor()
    sample = _sample("sample-corrupt", "2026-01-07")
    context = _context("replay-corrupt", sample, FaultScenario.CORRUPT_OUTPUT)

    first = await executor.execute(dna, sample, context)
    second = await executor.execute(dna, sample, context)

    assert first == second
    assert first.successful is False


def test_sandbox_policy_rejects_window_smaller_than_deadline() -> None:
    with pytest.raises(Exception, match="window must cover"):
        SandboxPolicy(max_virtual_seconds=10.0, deadline_seconds=300.0)


@pytest.mark.asyncio
async def test_sandbox_never_touches_the_caller_database(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    try:
        dna = DnaDefinition.from_workflow(
            MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
        )
        executor = quant_sandbox_executor()
        sample = _sample("sample-iso", "2026-01-08")
        measurement = await executor.execute(dna, sample, _context("r", sample, FaultScenario.NONE))
        assert measurement.successful is True
        rows = await database.fetch_all("SELECT COUNT(*) AS n FROM workflow_run")
        assert int(rows[0]["n"]) == 0, "sandbox must not persist runs into the caller database"
    finally:
        await database.close()
