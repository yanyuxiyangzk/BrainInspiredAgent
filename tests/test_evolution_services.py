"""E02/E03 service tests: dataset construction and governed candidate proposals.

Exercises the full pre-replay chain on real modules: seed the ACTIVE
baseline, register a candidate variant, seed shadow fitness for it, build
a dataset, propose the candidate through the governed generator, and run
the sandbox replay comparison.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from active_agent_platform.foundation import SystemClock, Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.candidate_service import (
    bump_version,
    parse_operations,
    propose_candidate,
)
from apps.quant_agent.dataset_service import build_experience_dataset
from apps.quant_agent.evolution_seed import DEFAULT_START, seed_baseline, seed_market_days
from apps.quant_agent.query_surface import CommandSurfaceQuery
from apps.quant_agent.sandbox import quant_sandbox_executor
from domain_sdk.dna import DnaDefinition, DnaParent, DnaStatus
from domain_sdk.dna_candidates import (
    CandidateOperationKind,
    CandidatePolicy,
    DnaCandidateGenerator,
)
from domain_sdk.dna_promotion import DnaPromotionController  # noqa: F401 - chain context
from domain_sdk.dna_replay import DnaSandboxReplay, ReplayPolicy, ReplayRequest
from domain_sdk.dna_repository import PersistentDnaRegistry
from domain_sdk.experience_dataset import ExperienceDatasetBuilder


def _title_input_operation(value: str) -> dict[str, object]:
    return {"kind": "SET_INPUT", "node_id": "build_summary", "field": "title", "value": value}


def _candidate_workflow(base: DnaDefinition) -> dict[str, object]:
    workflow = cast(
        "dict[str, object]", json.loads(json.dumps(base.to_document()["workflow"])),
    )
    nodes = cast("list[dict[str, object]]", workflow["nodes"])
    for node in nodes:
        if node.get("node_id") == "build_summary":
            cast("dict[str, object]", node["input"])["title"] = "Shadow summary"
    workflow["version"] = bump_version(base.version)
    return workflow


@pytest.mark.asyncio
async def _seed_baseline_and_candidate(
    database: SQLiteDatabase, tmp_path: Path,
) -> tuple[object, DnaDefinition, object]:
    """Seed the ACTIVE baseline plus a registered shadow candidate variant."""
    from apps.quant_agent.evolution_seed import SeedReport

    baseline_report: SeedReport = await seed_baseline(
        database, start=DEFAULT_START, days=4, artifacts_dir=tmp_path / "art-base",
    )
    base_row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? AND version=?",
        (baseline_report.dna_id, baseline_report.version),
    )
    assert base_row is not None
    base = DnaDefinition.from_document(
        cast("Mapping[str, object]", json.loads(str(base_row["document_json"])))
    )
    candidate_workflow = _candidate_workflow(base)
    candidate = DnaDefinition.from_workflow(
        candidate_workflow, dna_id=baseline_report.dna_id,
        version=bump_version(base.version), status=DnaStatus.CANDIDATE,
        parent_dna=(DnaParent(base.dna_id, base.version, base.content_digest),),
    )
    registry = PersistentDnaRegistry(database, clock := SystemClock(), Uuid7Generator(clock))
    await registry.register(candidate, correlation_id="test:register:candidate")
    candidate_days = await seed_market_days(
        database, workflow_document=candidate_workflow,
        dna_id=candidate.dna_id, version=candidate.version,
        content_digest=candidate.content_digest,
        start=DEFAULT_START, days=4, artifacts_dir=tmp_path / "art-cand",
        window_id=f"seed-{DEFAULT_START:%Y%m%d}-4",
        start_offset_seconds=2.0,
    )
    assert len(candidate_days) == 4 and all(day.successful for day in candidate_days)
    return baseline_report, base, candidate


@pytest.mark.asyncio
async def test_dataset_and_proposal_close_the_pre_replay_chain(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "chain.db")
    await database.initialize()
    clock = SystemClock()
    try:
        # 1-3. Baseline + registered candidate + shadow fitness.
        baseline_report, base, candidate = cast(
            "tuple[object, DnaDefinition, object]",
            await _seed_baseline_and_candidate(database, tmp_path),
        )

        # 4. E02: build the replay dataset over the shared window.
        result = await build_experience_dataset(
            database, dataset_id="market-summary-replay",
            window_id=f"seed-{DEFAULT_START:%Y%m%d}-4",
            baseline_content_digest=baseline_report.content_digest,
            candidate_content_digests=(candidate.content_digest,),
            starts_at=DEFAULT_START, ends_at=DEFAULT_START + timedelta(days=5),
        )
        assert result.sample_count == 8  # 4 baseline + 4 candidate days
        assert result.validation_count + result.test_count >= 1

        # 5. E03: propose the candidate through the governed generator.
        proposal = await propose_candidate(
            database, proposal_id="prop-e2e-1",
            operations=(_title_input_operation("Shadow summary"),),
            hypothesis="Friendlier title raises user value without extra cost",
            dataset_id=result.dataset_id, dataset_version=result.version,
        )
        assert proposal.candidate_version == bump_version(base.version)
        assert proposal.candidate_content_digest == candidate.content_digest

        # 6. Sandbox replay comparison over the dataset.
        dataset = await ExperienceDatasetBuilder(database, clock).get(
            result.dataset_id, result.version,
        )
        domain_proposal = await DnaCandidateGenerator(
            database, clock, CandidatePolicy(
                policy_version="candidate-service/1.0",
                mutable_paths=frozenset({"workflow.nodes.*.input.*"}),
                allowed_capabilities=frozenset({
                    "market.snapshot.read", "content.summary.generate",
                    "notification.local.send",
                }),
                allowed_bindings=frozenset({
                    ("market.snapshot.read", "1.0"), ("content.summary.generate", "1.0"),
                    ("notification.local.send", "1.0"),
                }),
                allowed_permissions=frozenset({"market.read", "notification.local.write"}),
                max_side_effect="IDEMPOTENT",
            ),
        ).get("prop-e2e-1")
        replay = await DnaSandboxReplay(
            database, clock, quant_sandbox_executor(),
            ReplayPolicy("replay/1.0", repetitions=2, minimum_cases=1),
        ).run(ReplayRequest(
            replay_id="replay-e2e-1", proposal=domain_proposal,
            parent=base, dataset=dataset, correlation_id="cli:replay:e2e",
        ))
        assert replay.candidate.success_rate >= 0
        assert replay.candidate.average_cost_minor >= 0
        assert replay.report_digest.startswith("sha256:")

        # 7. The replay query surface exposes the full report, vectors and cases.
        surface = CommandSurfaceQuery(database)
        listing = await surface.evolution("replay", 20, "replay-e2e-1")
        assert listing["evidence_source"] == "append-only replay run and cases"
        assert listing["cases"] and len(listing["cases"]) >= 1
        report = listing["report"]
        assert report["status"] in {"PASSED", "FAILED"}
        vectors = listing["vectors"]
        assert vectors["parent"]["success_rate"] == vectors["candidate"]["success_rate"]
        assert "cost_increase_ratio" in vectors["deltas"]
        assert isinstance(listing["reasons"], list)
    finally:
        await database.close()


def test_parse_operations_maps_documents() -> None:
    operations = parse_operations((_title_input_operation("X"),))
    assert operations[0].kind is CandidateOperationKind.SET_INPUT
    assert operations[0].node_id == "build_summary"
    assert operations[0].field == "title"


def test_bump_version_increments_patch() -> None:
    assert bump_version("1.0.0") == "1.0.1"
    with pytest.raises(Exception, match="semantic"):
        bump_version("1.0")


def test_cli_evolution_views_are_declared() -> None:
    from apps.quant_agent.commands import COMMANDS

    assert "/evolution" in COMMANDS


@pytest.mark.asyncio
async def test_cli_build_dataset_and_propose_roundtrip(tmp_path: Path) -> None:
    from io import StringIO

    from apps.quant_agent.cli import run as run_cli

    database_path = tmp_path / "cli.db"
    seed_database = SQLiteDatabase(database_path)
    await seed_database.initialize()
    try:
        _report, _base, candidate = await _seed_baseline_and_candidate(
            seed_database, tmp_path,
        )
    finally:
        await seed_database.close()

    operations = json.dumps([_title_input_operation("Shadow summary")])
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(database_path), "evolution", "propose", "prop-cli-1",
         "--operations", operations, "--hypothesis", "Title variant",
         "--dataset-id", "missing-ds", "--dataset-version", "1.0.0"),
        stdout, stderr,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "REJECTED"  # dataset missing → service rejects

    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(database_path), "evolution", "build-dataset",
         "cli-dataset", "--window-days", "4",
         "--candidate-digests", candidate.content_digest),
        stdout, stderr,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "BUILT" and payload["sample_count"] == 8

    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(database_path), "evolution", "propose", "prop-cli-2",
         "--operations", operations, "--hypothesis", "Title variant",
         "--dataset-id", "cli-dataset", "--dataset-version", "1.0.0"),
        stdout, stderr,
    )
    assert code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "PROPOSED"
    assert payload["candidate_content_digest"] == candidate.content_digest
