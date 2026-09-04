"""Coverage completion tests for the evolution line (E00-E05).

Targets the remaining untested branches: sandbox fault paths and policy
validation, seed error paths and CLI, driver edge strategies, candidate
service rejections, and dataset service validation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.foundation import SystemClock, Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent import MARKET_SUMMARY_WORKFLOW
from apps.quant_agent.candidate_service import (
    CandidateServiceError,
    parse_operations,
    propose_candidate,
)
from apps.quant_agent.dataset_service import (
    DatasetServiceError,
    build_experience_dataset,
    split_window,
)
from apps.quant_agent.evolution_seed import DEFAULT_START, seed_baseline, seed_market_days
from apps.quant_agent.sandbox import quant_sandbox_executor
from domain_sdk.dna import DnaDefinition, DnaParent, DnaStatus
from domain_sdk.dna_candidates import CandidateOperationKind
from domain_sdk.dna_evolution_driver import (
    EvolutionDriver,
    LlmEvolutionStrategy,
    detect_weakness,
)
from domain_sdk.dna_replay import FaultScenario
from domain_sdk.dna_repository import PersistentDnaRegistry
from domain_sdk.dna_sandbox_executor import SandboxPolicy
from domain_sdk.experience_dataset import DatasetCohort, DatasetSplit, ExperienceSample

NOW = datetime(2026, 1, 5, 1, 25, tzinfo=UTC)


def _sample(sample_id: str, trade_date: str, *, flat: bool = False) -> ExperienceSample:
    parameters = {"symbols": ["INDEX.TEST"], "trade_date": trade_date, "title": "Sandbox"}
    document: dict[str, object] = (
        dict(parameters) if flat else {"parameters": parameters}
    )
    digest = "sha256:" + "0" * 64
    return ExperienceSample(
        sample_id=sample_id, ordinal=1, split=DatasetSplit.VALIDATION,
        cohort=DatasetCohort.BASELINE, dna_id="workflow.market_summary",
        dna_version="1.0.0", content_digest=digest, evaluation_id=f"eval-{sample_id}",
        observation_id=f"obs-{sample_id}", observed_at=NOW, sample_digest=digest,
        document=document,
    )


def _context(replay_id: str, sample: ExperienceSample, fault: FaultScenario) -> object:
    from domain_sdk.dna_replay import ReplayContext

    return ReplayContext(replay_id, sample.sample_id, NOW, "seed", fault)


def _dna() -> DnaDefinition:
    return DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
    )


@pytest.mark.asyncio
async def test_sandbox_timeout_fault_terminates_within_virtual_window() -> None:
    executor = quant_sandbox_executor()
    sample = _sample("t1", "2026-01-10")
    measurement = await executor.execute(
        _dna(), sample, _context("r-t1", sample, FaultScenario.TIMEOUT),
    )
    assert measurement.successful is False and measurement.stable is False


@pytest.mark.asyncio
async def test_sandbox_cancelled_fault_reports_cancellation() -> None:
    executor = quant_sandbox_executor()
    sample = _sample("t2", "2026-01-11")
    measurement = await executor.execute(
        _dna(), sample, _context("r-t2", sample, FaultScenario.CANCELLED),
    )
    assert measurement.successful is False


def test_sandbox_policy_rejects_non_positive_quantum() -> None:
    with pytest.raises(Exception, match="quantum and window"):
        SandboxPolicy(quantum_seconds=0.0)


def test_sandbox_policy_rejects_non_positive_deadline() -> None:
    with pytest.raises(Exception, match="deadline must be positive"):
        SandboxPolicy(deadline_seconds=0.0)


@pytest.mark.asyncio
async def test_sandbox_accepts_flat_sample_documents() -> None:
    executor = quant_sandbox_executor()
    sample = _sample("t3", "2026-01-12", flat=True)
    measurement = await executor.execute(
        _dna(), sample, _context("r-t3", sample, FaultScenario.NONE),
    )
    assert measurement.successful is True


# ---------------------------------------------------------------------------
# evolution_seed error paths and CLI entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_rejects_naive_start(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "naive.db")
    await database.initialize()
    try:
        naive = datetime(2026, 1, 5, 1, 25)  # noqa: DTZ001 - naive datetime is the point
        with pytest.raises(Exception, match="timezone-aware"):
            await seed_baseline(database, start=naive, days=1)
    finally:
        await database.close()


def test_seed_module_cli_prints_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.quant_agent.evolution_seed as seed_module

    database_path = tmp_path / "cli.db"
    monkeypatch.setattr("sys.argv", ["evolution-seed", "--database", str(database_path),
                                     "--days", "2"])
    assert asyncio.run(seed_module._main()) == 0


# ---------------------------------------------------------------------------
# evolution driver edge branches
# ---------------------------------------------------------------------------


def _snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "version": "1.0.0", "revision": 1, "readiness": "READY",
        "success_rate": 1.0, "evidence_score": 1.0, "user_value_score": 1.0,
        "stability_rate": 1.0, "risk_rate": 0.0,
    }
    snapshot.update(overrides)
    return snapshot


def test_driver_hypothesis_reports_observed_value() -> None:
    from domain_sdk.dna_evolution_driver import RuleEvolutionStrategy

    hypothesis = RuleEvolutionStrategy().hypothesis(
        "evidence_score", _snapshot(evidence_score=0.6),
    )
    assert "0.600" in hypothesis and "0.80" in hypothesis

    with pytest.raises(KeyError):
        # detect_weakness never emits latency/risk; hypothesis reads the metric.
        RuleEvolutionStrategy().hypothesis("latency", _snapshot())


def test_driver_operations_document_serializes() -> None:
    plan = asyncio.run(EvolutionDriver().drive(
        DnaDefinition.from_workflow(
            MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
        ),
        snapshot=_snapshot(user_value_score=0.4),
    ))
    document = plan.operations_document()
    assert document[0]["kind"] == "SET_INPUT"


def test_driver_non_semantic_version_falls_back_to_suffix() -> None:
    plan = asyncio.run(EvolutionDriver().drive(
        DnaDefinition.from_workflow(
            MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
        ),
        snapshot=_snapshot(user_value_score=0.4, version="dev"),
    ))
    assert plan.new_version == "dev.1"


def test_llm_strategy_parses_non_mapping_json_as_fallback() -> None:
    baseline = DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
    )

    class ListModel:
        async def generate(self, request: object) -> str:
            return "[1, 2]"

    hypothesis, _, source = asyncio.run(LlmEvolutionStrategy(ListModel()).plan(
        "user_value_score", _snapshot(user_value_score=0.4), baseline,
    ))
    assert source == "rule-fallback" and "user_value_score" in hypothesis


def test_llm_strategy_uses_constraint_field_for_latency() -> None:
    baseline = DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
    )

    class LatencyModel:
        async def generate(self, request: object) -> dict[str, object]:
            return {"hypothesis": "Tighter latency", "field": "max_latency_ms",
                    "value": 4000}

    _, operations, source = asyncio.run(LlmEvolutionStrategy(LatencyModel()).plan(
        "stability_rate", _snapshot(stability_rate=0.8), baseline,
    ))
    assert source == "structured-model"
    assert operations[0].kind is CandidateOperationKind.SET_CONSTRAINT
    assert operations[0].value == 4000


def test_detect_weakness_none_when_all_healthy() -> None:
    assert detect_weakness(_snapshot()) is None


# ---------------------------------------------------------------------------
# candidate service rejections
# ---------------------------------------------------------------------------


async def _prepared(tmp_path: Path) -> tuple[SQLiteDatabase, dict[str, object], str]:
    database = SQLiteDatabase(tmp_path / "cand.db")
    await database.initialize()
    report = await seed_baseline(database, start=DEFAULT_START, days=3,
                                 artifacts_dir=tmp_path / "art")
    base_row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? AND version=?",
        (report.dna_id, report.version),
    )
    base = DnaDefinition.from_document(json.loads(str(base_row["document_json"])))  # type: ignore[index]
    workflow = cast_workflow(base)
    for node in workflow["nodes"]:
        if node.get("node_id") == "build_summary":
            node["input"]["title"] = "Shadow summary"
    workflow["version"] = "1.0.1"
    candidate = DnaDefinition.from_workflow(
        workflow, dna_id=report.dna_id, version="1.0.1", status=DnaStatus.CANDIDATE,
        parent_dna=(DnaParent(base.dna_id, base.version, base.content_digest),),
    )
    clock = SystemClock()
    registry = PersistentDnaRegistry(database, clock, Uuid7Generator(clock))
    await registry.register(candidate, correlation_id="test:register")
    await seed_market_days(
        database, workflow_document=workflow, dna_id=candidate.dna_id,
        version=candidate.version, content_digest=candidate.content_digest,
        start=DEFAULT_START, days=3, artifacts_dir=tmp_path / "art-c",
        window_id=report.window_id, start_offset_seconds=2.0,
        title="Shadow summary",
    )
    dataset = await build_experience_dataset(
        database, dataset_id="ds-cand", window_id=report.window_id,
        baseline_content_digest=report.content_digest,
        candidate_content_digests=(candidate.content_digest,),
        starts_at=DEFAULT_START, ends_at=DEFAULT_START + timedelta(days=4),
    )
    operations = [{"kind": "SET_INPUT", "node_id": "build_summary",
                   "field": "title", "value": "Shadow summary"}]
    return database, {"dataset_id": dataset.dataset_id,
                      "dataset_version": dataset.version}, operations


def cast_workflow(base: DnaDefinition) -> dict[str, object]:
    return json.loads(json.dumps(base.to_document()["workflow"]))


@pytest.mark.asyncio
async def test_propose_rejects_duplicate_content(tmp_path: Path) -> None:
    database, dataset, operations = await _prepared(tmp_path)
    try:
        first = await propose_candidate(
            database, proposal_id="prop-dup-1", operations=operations,
            hypothesis="H", dataset_id=dataset["dataset_id"],
            dataset_version=dataset["dataset_version"],
        )
        assert first.to_dict()["candidate_status"] == "CANDIDATE"
        with pytest.raises(CandidateServiceError, match="already has a proposal"):
            await propose_candidate(
                database, proposal_id="prop-dup-2", operations=operations,
                hypothesis="H2", dataset_id=dataset["dataset_id"],
                dataset_version=dataset["dataset_version"],
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_propose_rejects_bad_operations_and_mode(tmp_path: Path) -> None:
    database, dataset, _ = await _prepared(tmp_path)
    try:
        with pytest.raises(CandidateServiceError, match="operation kind"):
            await propose_candidate(
                database, proposal_id="prop-kind-1",
                operations=[{"kind": "NOT_A_KIND", "node_id": "x"}],
                hypothesis="H", dataset_id=dataset["dataset_id"],
                dataset_version=dataset["dataset_version"],
            )
        with pytest.raises(CandidateServiceError, match="requires a donor"):
            await propose_candidate(
                database, proposal_id="prop-xover-1",
                operations=[{"kind": "REPLACE_FROM_DONOR", "node_id": "build_summary"}],
                hypothesis="H", dataset_id=dataset["dataset_id"],
                dataset_version=dataset["dataset_version"], mode="CROSSOVER",
            )
        with pytest.raises(CandidateServiceError, match="mode must be"):
            await propose_candidate(
                database, proposal_id="prop-mode-1", operations=[], hypothesis="H",
                dataset_id=dataset["dataset_id"],
                dataset_version=dataset["dataset_version"], mode="MAGIC",
            )
        with pytest.raises(CandidateServiceError, match="not mutable"):
            await propose_candidate(
                database, proposal_id="prop-mut-1",
                operations=[{"kind": "SET_CONSTRAINT", "node_id": "notify",
                             "field": "side_effect", "value": "NON_REPLAYABLE"}],
                hypothesis="H", dataset_id=dataset["dataset_id"],
                dataset_version=dataset["dataset_version"],
            )
    finally:
        await database.close()


def test_parse_operations_rejects_unknown_kind() -> None:
    with pytest.raises(CandidateServiceError, match="operation kind"):
        parse_operations([{"kind": "WAT", "node_id": "x"}])


# ---------------------------------------------------------------------------
# dataset service validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dataset_rejects_naive_window_and_empty_candidates(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ds.db")
    await database.initialize()
    try:
        with pytest.raises(DatasetServiceError, match="timezone-aware"):
            naive_start, naive_end = datetime(2026, 1, 1), datetime(2026, 1, 5)  # noqa: DTZ001
            await build_experience_dataset(
                database, dataset_id="ds-naive", window_id="w",
                baseline_content_digest="sha256:" + "0" * 64,
                candidate_content_digests=("sha256:" + "1" * 64,),
                starts_at=naive_start, ends_at=naive_end,
            )
        with pytest.raises(DatasetServiceError, match="at least one candidate"):
            await build_experience_dataset(
                database, dataset_id="ds-empty", window_id="w",
                baseline_content_digest="sha256:" + "0" * 64,
                candidate_content_digests=(),
                starts_at=DEFAULT_START, ends_at=DEFAULT_START + timedelta(days=1),
            )
    finally:
        await database.close()


def test_split_window_returns_60_20_20_boundaries() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 11, tzinfo=UTC)
    train_until, validation_until = split_window(start, end)
    assert train_until == start + timedelta(days=6)
    assert validation_until == start + timedelta(days=8)
