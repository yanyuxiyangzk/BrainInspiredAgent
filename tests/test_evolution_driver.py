"""E05 evolution driver tests: weakness detection and governed plan output."""

from __future__ import annotations

import pytest

from apps.quant_agent import MARKET_SUMMARY_WORKFLOW
from domain_sdk.dna import DnaDefinition, DnaStatus
from domain_sdk.dna_candidates import CandidateOperationKind
from domain_sdk.dna_evolution_driver import (
    EvolutionDriver,
    EvolutionDriverError,
    LlmEvolutionStrategy,
    detect_weakness,
)

SNAPSHOT = {
    "version": "1.0.0",
    "revision": 3,
    "readiness": "READY",
    "success_rate": 1.0,
    "evidence_score": 1.0,
    "user_value_score": 1.0,
    "stability_rate": 1.0,
    "risk_rate": 0.0,
}


def _baseline() -> DnaDefinition:
    return DnaDefinition.from_workflow(
        MARKET_SUMMARY_WORKFLOW, dna_id="workflow.market_summary", version="1.0.0",
        status=DnaStatus.ACTIVE,
    )


def _weak_snapshot(**overrides: object) -> dict[str, object]:
    snapshot = dict(SNAPSHOT)
    snapshot.update(overrides)
    return snapshot


def test_detect_weakness_ranks_below_threshold_metrics() -> None:
    snapshot = _weak_snapshot(evidence_score=0.7, user_value_score=0.4)
    assert detect_weakness(snapshot) == "user_value_score"
    assert detect_weakness(_weak_snapshot()) is None


def test_rule_driver_emits_governed_operations_for_value_weakness() -> None:
    plan = asyncio_run_plan(_weak_snapshot(user_value_score=0.4))
    assert plan.weakness == "user_value_score"
    assert plan.source == "rule"
    assert plan.new_version == "1.0.1"
    assert plan.snapshot_revision == 3
    assert plan.operations[0].kind is CandidateOperationKind.SET_INPUT
    assert plan.operations[0].node_id == "build_summary"


def test_rule_driver_emits_constraint_for_latency_weakness() -> None:
    plan = asyncio_run_plan(_weak_snapshot(stability_rate=0.8))
    assert plan.weakness == "stability_rate"
    assert plan.operations[0].kind is CandidateOperationKind.SET_INPUT


@pytest.mark.asyncio
async def test_driver_rejects_healthy_baseline() -> None:
    driver = EvolutionDriver()
    with pytest.raises(EvolutionDriverError, match="nothing to evolve"):
        await driver.drive(_baseline(), snapshot=_weak_snapshot())


@pytest.mark.asyncio
async def test_driver_rejects_risk_blocked_baseline() -> None:
    driver = EvolutionDriver()
    with pytest.raises(EvolutionDriverError, match="risk blocked"):
        await driver.drive(_baseline(), snapshot=_weak_snapshot(readiness="RISK_BLOCKED"))


@pytest.mark.asyncio
async def test_llm_strategy_refines_hypothesis_and_falls_back() -> None:
    baseline = _baseline()
    snapshot = _weak_snapshot(user_value_score=0.4)

    class GoodModel:
        async def generate(self, request: object) -> dict[str, object]:
            return {"hypothesis": " Friendlier titles win more clicks ",
                    "field": "title", "node_id": "build_summary", "value": "Friendly"}

    hypothesis, operations, source = await LlmEvolutionStrategy(GoodModel()).plan(
        "user_value_score", snapshot, baseline,
    )
    assert source == "structured-model"
    assert hypothesis == "Friendlier titles win more clicks"
    assert operations[0].value == "Friendly"

    class BrokenModel:
        async def generate(self, request: object) -> dict[str, object]:
            raise RuntimeError("model down")

    hypothesis, operations, source = await LlmEvolutionStrategy(BrokenModel()).plan(
        "user_value_score", snapshot, baseline,
    )
    assert source == "rule-fallback"
    assert hypothesis.startswith("Raise user_value_score")

    class GarbageModel:
        async def generate(self, request: object) -> str:
            return "not json"

    hypothesis, _, source = await LlmEvolutionStrategy(GarbageModel()).plan(
        "user_value_score", snapshot, baseline,
    )
    assert source == "rule-fallback" and hypothesis.startswith("Raise user_value_score")


def asyncio_run_plan(snapshot: dict[str, object]):
    import asyncio

    async def run() -> object:
        return await EvolutionDriver().drive(_baseline(), snapshot=snapshot)

    return asyncio.run(run())
