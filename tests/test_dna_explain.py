from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_dna_promotion import setup_campaign

from domain_sdk import (
    DnaExplainError,
    DnaLineageExplainer,
    ExplainRequest,
    PromotionObservation,
)


@pytest.mark.asyncio
async def test_explanation_connects_lineage_generation_replay_selection_and_promotion(
    tmp_path: Path,
) -> None:
    database, _, clock, controller, campaign, proposal, _ = await setup_campaign(tmp_path)
    campaign = await controller.observe(PromotionObservation(
        "explain-observation", campaign.campaign_id, True, True, clock.now(),
        "explain-correlation",
    ))
    campaign = await controller.kill(
        campaign.campaign_id, reason="explain stop", correlation_id="explain-correlation",
    )
    explainer = DnaLineageExplainer(database, clock)
    request = ExplainRequest(
        "explanation-full", campaign.dna_id, campaign.dna_version, "explain-correlation",
    )
    explanation = await explainer.explain(request)
    assert explanation.content_digest == proposal.candidate.content_digest
    assert explanation.document["generation"]["proposal_id"] == proposal.proposal_id  # type: ignore[index]
    assert len(explanation.document["lineage"]) == 2  # type: ignore[arg-type]
    assert explanation.document["replays"][0]["status"] == "PASSED"  # type: ignore[index]
    assert explanation.document["selections"][0]["disposition"] == "SELECTED"  # type: ignore[index]
    assert explanation.document["promotions"][0]["stage"] == "STOPPED"  # type: ignore[index]
    assert any(reason == "promotion:STOPPED" for reason in explanation.why)
    assert any(reason.startswith("promotion_last_reason:kill_switch:")
               for reason in explanation.why)
    assert await explainer.explain(request) == explanation
    assert await explainer.get(request.explanation_id) == explanation
    with pytest.raises(DnaExplainError, match="another DNA"):
        await explainer.explain(replace(request, version=proposal.candidate.parent_dna[0].version))
    await database.close()


@pytest.mark.asyncio
async def test_root_explanation_and_snapshot_tamper_detection(tmp_path: Path) -> None:
    database, _, clock, _, campaign, proposal, _ = await setup_campaign(tmp_path)
    parent = proposal.candidate.parent_dna[0]
    explainer = DnaLineageExplainer(database, clock)
    explanation = await explainer.explain(ExplainRequest(
        "explanation-root", parent.dna_id, parent.version, "correlation",
    ))
    assert explanation.why[0] == "registered_or_composed_without_H06_proposal"
    assert len(explanation.document["lineage"]) == 1  # type: ignore[arg-type]
    assert explanation.document["generation"] is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_explanation WHERE explanation_id=?",
                (explanation.explanation_id,),
            )
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_explanation_no_update")
        await transaction.execute(
            "UPDATE dna_explanation SET content_digest='sha256:bad' WHERE explanation_id=?",
            (explanation.explanation_id,),
        )
    with pytest.raises(DnaExplainError, match="target mismatch"):
        await explainer.get(explanation.explanation_id)
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_explanation SET content_digest=? WHERE explanation_id=?",
            (explanation.content_digest, explanation.explanation_id),
        )
        await transaction.execute(
            "UPDATE dna_explanation SET explanation_digest='sha256:bad' WHERE explanation_id=?",
            (explanation.explanation_id,),
        )
    with pytest.raises(DnaExplainError, match="digest mismatch"):
        await explainer.get(explanation.explanation_id)
    with pytest.raises(DnaExplainError, match="not found"):
        await explainer.get("missing-explanation")
    assert campaign.stage.value == "SHADOW"
    await database.close()


@pytest.mark.asyncio
async def test_explanation_rejects_tampered_generation_fact(tmp_path: Path) -> None:
    database, _, clock, _, campaign, proposal, _ = await setup_campaign(tmp_path)
    with pytest.raises(DnaExplainError, match="lineage member is missing"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-missing-dna", "missing-dna", "1.0.0", "correlation",
        ))
    dna_row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? AND version=?",
        (campaign.dna_id, campaign.dna_version),
    )
    assert dna_row is not None
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET document_json='{}' WHERE dna_id=? AND version=?",
            (campaign.dna_id, campaign.dna_version),
        )
    with pytest.raises(DnaExplainError, match="lineage document is invalid"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-dna", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET document_json=? WHERE dna_id=? AND version=?",
            (str(dna_row["document_json"]), campaign.dna_id, campaign.dna_version),
        )
    proposal_row = await database.fetch_one(
        "SELECT candidate_document_json FROM dna_candidate_proposal WHERE proposal_id=?",
        (proposal.proposal_id,),
    )
    assert proposal_row is not None
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_candidate_proposal_no_update")
        await transaction.execute(
            "UPDATE dna_candidate_proposal SET candidate_document_json='{}' WHERE proposal_id=?",
            (proposal.proposal_id,),
        )
    with pytest.raises(DnaExplainError, match="Candidate document is invalid"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-candidate", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_candidate_proposal SET candidate_document_json=? WHERE proposal_id=?",
            (str(proposal_row["candidate_document_json"]), proposal.proposal_id),
        )
        await transaction.execute(
            "UPDATE dna_candidate_proposal SET proposal_digest='sha256:bad' WHERE proposal_id=?",
            (proposal.proposal_id,),
        )
    with pytest.raises(DnaExplainError, match="proposal digest mismatch"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-tamper", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    await database.close()


@pytest.mark.asyncio
async def test_explanation_rejects_tampered_decision_evidence(tmp_path: Path) -> None:
    for name in ("replay", "selection", "promotion"):
        (tmp_path / name).mkdir()

    database, _, clock, _, campaign, proposal, _ = await setup_campaign(tmp_path / "replay")
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_replay_run_no_update")
        await transaction.execute(
            "UPDATE dna_replay_run SET report_digest='sha256:bad' WHERE proposal_id=?",
            (proposal.proposal_id,),
        )
    with pytest.raises(DnaExplainError, match="Replay report digest"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-replay", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    await database.close()

    database, _, clock, _, campaign, proposal, _ = await setup_campaign(tmp_path / "selection")
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_selection_run_no_update")
        await transaction.execute(
            "UPDATE dna_selection_run SET report_digest='sha256:bad'",
        )
    with pytest.raises(DnaExplainError, match="Selection report digest"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-selection-report", campaign.dna_id, campaign.dna_version,
            "correlation",
        ))
    selection = await database.fetch_one(
        "SELECT report_json FROM dna_selection_run LIMIT 1",
    )
    assert selection is not None
    report_digest = "sha256:" + hashlib.sha256(str(selection["report_json"]).encode()).hexdigest()
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_selection_run SET report_digest=?", (report_digest,),
        )
        await transaction.execute("DROP TRIGGER dna_selection_member_no_update")
        await transaction.execute(
            "UPDATE dna_selection_member SET member_digest='sha256:bad' WHERE proposal_id=?",
            (proposal.proposal_id,),
        )
    with pytest.raises(DnaExplainError, match="Selection member digest"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-selection", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    await database.close()

    database, _, clock, controller, campaign, _, _ = await setup_campaign(
        tmp_path / "promotion",
    )
    await controller.observe(PromotionObservation(
        "tampered-observation", campaign.campaign_id, True, True, clock.now(), "correlation",
    ))
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_promotion_observation_no_update")
        await transaction.execute(
            """UPDATE dna_promotion_observation SET observation_digest='sha256:bad'
               WHERE observation_id='tampered-observation'""",
        )
    with pytest.raises(DnaExplainError, match="Promotion observation digest"):
        await DnaLineageExplainer(database, clock).explain(ExplainRequest(
            "explanation-bad-promotion", campaign.dna_id, campaign.dna_version, "correlation",
        ))
    await database.close()


def test_explanation_request_contract() -> None:
    with pytest.raises(DnaExplainError, match="metadata"):
        ExplainRequest("!", "", "", "")
