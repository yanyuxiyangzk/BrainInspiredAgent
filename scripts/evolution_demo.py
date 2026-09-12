"""Generic-branch DNA evolution demo: auto-plan and the governed promotion pipeline.

Runs the real SDK modules against a throwaway SQLite database:

(A) AUTO-PLAN — fitness weakness → EvolutionDriver → governed candidate
    proposal (E05 + E06, no human operations/hypothesis).
(B) GOVERNED PIPELINE — population → sandbox replay → selection →
    promotion campaign to SHADOW/CANARY (E07 + E08 + E09).

Not part of CI; prints a JSON evidence summary per stage.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_dna_candidates import policy as candidate_policy
from test_dna_candidates import setup as candidate_setup
from test_evolution_driver import SNAPSHOT

from domain_sdk import (
    CandidateMode,
    CandidateRequest,
    DnaPopulationSelector,
    PopulationCandidate,
    PromotionObservation,
    SelectionPolicy,
    SelectionRequest,
)
from domain_sdk.dna_evolution_driver import EvolutionDriver, detect_weakness


async def stage_a_auto_plan(workdir: Path) -> dict[str, object]:
    """(A) Fitness weakness → auto plan → governed candidate proposal."""
    database, base, _donor, dataset, generator = await candidate_setup(workdir)
    try:
        weak = dict(SNAPSHOT)
        weak["user_value_score"] = 0.42  # dominate weakness below the 0.70 target
        weakness = detect_weakness(weak)
        plan = await EvolutionDriver(database).drive(base, snapshot=weak)
        request = CandidateRequest(
            proposal_id="demo-auto-plan-1", mode=CandidateMode.MUTATION,
            base=base, new_version=plan.new_version, dataset=dataset,
            hypothesis=plan.hypothesis, operations=plan.operations,
            correlation_id="demo:auto-plan",
        )
        proposal = await generator.generate(request)
        mutated_node = next(
            node for node in proposal.candidate.workflow["nodes"]  # type: ignore[index]
            if node["node_id"] == "summary"  # type: ignore[index]
        )
        return {
            "weakness": weakness,
            "plan_source": plan.source,
            "hypothesis": plan.hypothesis,
            "operations": [op for op in plan.operations_document()],
            "proposal_id": proposal.proposal_id,
            "candidate_version": proposal.candidate.version,
            "candidate_status": proposal.candidate.status.value,
            "mutated_title": mutated_node["input"].get("title"),  # type: ignore[union-attr]
            "generator_policy": candidate_policy().policy_version,
        }
    finally:
        await database.close()


async def stage_b_governed_pipeline(workdir: Path) -> dict[str, object]:
    """(B) Population → replay → selection → promotion to shadow/canary."""
    from test_dna_promotion import setup_campaign
    from test_dna_selection import setup_population

    population_dir = workdir / "population"
    population_dir.mkdir(exist_ok=True)
    database, proposals, reports = await setup_population(population_dir)
    await database.close()

    campaign_dir = workdir / "campaign"
    campaign_dir.mkdir(exist_ok=True)
    database, _registry, clock, controller, campaign, _proposal, _proposals = (
        await setup_campaign(campaign_dir)
    )
    try:
        selector = DnaPopulationSelector(
            database, clock, SelectionPolicy("selection/1.0", maximum_survivors=1),
        )
        selection = await selector.select(SelectionRequest(
            "demo-selection",
            tuple(PopulationCandidate(p, r) for p, r in zip(proposals, reports, strict=True)),
            "demo-correlation",
        ))
        dispositions = {
            item.proposal_id: item.disposition.value for item in selection.members
        }
        replay_vectors = [
            {
                "proposal": report.proposal_id,
                "verdict": report.status.value,
                "parent": {
                    "success": round(report.parent.success_rate, 3),
                    "evidence": round(report.parent.evidence_score, 3),
                    "value": round(report.parent.user_value_score, 3),
                    "cost_minor": report.parent.average_cost_minor,
                },
                "candidate": {
                    "success": round(report.candidate.success_rate, 3),
                    "evidence": round(report.candidate.evidence_score, 3),
                    "value": round(report.candidate.user_value_score, 3),
                    "cost_minor": report.candidate.average_cost_minor,
                },
                "deltas": {
                    key: round(value, 3) for key, value in report.deltas.items()
                },
                "reasons": list(report.reasons),
            }
            for report in reports
        ]
        # Shadow observations promote the campaign to CANARY.
        stage_before = campaign.stage.value
        for ordinal in range(2):
            campaign = await controller.observe(PromotionObservation(
                f"demo-shadow-{ordinal}", campaign.campaign_id, True, True,
                clock.now(), "demo",
            ))
        return {
            "population": [p.proposal_id for p in proposals],
            "replay": replay_vectors,
            "selection": {
                "survivors": list(selection.selected_proposal_ids),
                "dispositions": dispositions,
            },
            "campaign_id": campaign.campaign_id,
            "stage_before_observations": stage_before,
            "stage_after_shadow_observations": campaign.stage.value,
            "campaign_revision": campaign.revision,
            "promotion_policy_version": "promotion/1.0",
        }
    finally:
        await database.close()


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evolution-demo-"))
    (workdir / "a").mkdir()
    (workdir / "b").mkdir()
    stage_a = await stage_a_auto_plan(workdir / "a")
    stage_b = await stage_b_governed_pipeline(workdir / "b")
    print(json.dumps({
        "branch": "generic-core",
        "stage_a_auto_plan": stage_a,
        "stage_b_governed_pipeline": stage_b,
    }, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
