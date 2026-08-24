from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from test_dna_candidates import START
from test_dna_selection import setup_population

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from apps.quant_agent.cli import run
from domain_sdk import (
    DnaPopulationSelector,
    DnaPromotionController,
    DnaPromotionError,
    DnaStatus,
    PersistentDnaRegistry,
    PopulationCandidate,
    PromotionObservation,
    PromotionPolicy,
    PromotionRequest,
    PromotionRoute,
    PromotionStage,
    SelectionPolicy,
    SelectionRequest,
)


async def setup_campaign(tmp_path: Path):  # type: ignore[no-untyped-def]
    database, proposals, reports = await setup_population(tmp_path)
    selector = DnaPopulationSelector(
        database, FakeClock(START), SelectionPolicy("selection/h09", maximum_survivors=1),
    )
    selection = await selector.select(SelectionRequest(
        "promotion-selection", tuple(PopulationCandidate(proposal, report)
                                     for proposal, report in zip(proposals, reports, strict=True)),
        "promotion-correlation",
    ))
    selected_id = selection.selected_proposal_ids[0]
    proposal = next(item for item in proposals if item.proposal_id == selected_id)
    clock = FakeClock(START + timedelta(hours=8))
    registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(1000, 1100)),
    )
    parent = proposal.candidate.parent_dna[0]
    baseline = await registry.get(parent.dna_id, parent.version)
    for status in (DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY):
        baseline = await registry.transition(
            parent.dna_id, parent.version, status, expected_revision=baseline.revision,
            reason="prepare active baseline", correlation_id="promotion-correlation",
        )
    baseline = await registry.activate(
        parent.dna_id, parent.version, expected_revision=baseline.revision,
        reason="prepare active baseline", correlation_id="promotion-correlation",
    )
    controller = DnaPromotionController(
        database, registry, clock,
        FakeUuidGenerator(UUID(int=value) for value in range(2000, 2100)),
        PromotionPolicy("promotion/1.0", 2, 2, timedelta(0), timedelta(0), 0.25),
    )
    campaign = await controller.start(PromotionRequest(
        "campaign-main", selection.selection_id, proposal, "promotion-correlation",
    ))
    return database, registry, clock, controller, campaign, proposal, proposals


@pytest.mark.asyncio
async def test_shadow_canary_active_and_hard_failure_automatic_rollback(tmp_path: Path) -> None:
    database, registry, clock, controller, campaign, proposal, _ = await setup_campaign(tmp_path)
    assert campaign.stage is PromotionStage.SHADOW
    request = PromotionRequest(
        campaign.campaign_id, campaign.selection_id, proposal, "promotion-correlation",
    )
    assert await controller.start(request) == campaign
    assert await controller.route(campaign.campaign_id, "account-1") \
        is PromotionRoute.SHADOW_MIRROR
    first = PromotionObservation(
        "shadow-0", campaign.campaign_id, True, True, clock.now(), "correlation",
    )
    campaign = await controller.observe(first)
    assert await controller.observe(first) == campaign
    with pytest.raises(DnaPromotionError, match="another payload"):
        await controller.observe(PromotionObservation(
            "shadow-0", campaign.campaign_id, False, True, clock.now(), "correlation",
        ))
    campaign = await controller.observe(PromotionObservation(
        "shadow-1", campaign.campaign_id, True, True, clock.now(), "correlation",
    ))
    assert campaign.stage is PromotionStage.CANARY
    assert await controller.route(campaign.campaign_id, "account-1") in {
        PromotionRoute.BASELINE, PromotionRoute.CANARY,
    }
    for ordinal in range(2):
        campaign = await controller.observe(PromotionObservation(
            f"canary-{ordinal}", campaign.campaign_id, True, True, clock.now(), "correlation",
        ))
    assert campaign.stage is PromotionStage.ACTIVE
    assert await controller.route(campaign.campaign_id, "account-1") is PromotionRoute.ACTIVE
    assert (await registry.get(proposal.candidate.dna_id,
                               proposal.candidate.version)).dna.status is DnaStatus.ACTIVE
    campaign = await controller.observe(PromotionObservation(
        "active-healthy", campaign.campaign_id, True, True, clock.now(), "correlation",
    ))
    assert campaign.stage is PromotionStage.ACTIVE
    campaign = await controller.observe(PromotionObservation(
        "active-risk", campaign.campaign_id, True, True, clock.now(), "correlation",
        risk_violations=("permission_violation",),
    ))
    assert campaign.stage is PromotionStage.ROLLED_BACK
    assert (await registry.get(campaign.dna_id,
                               campaign.baseline_version or "")).dna.status is DnaStatus.ACTIVE
    assert await controller.route(campaign.campaign_id, "account-1") is PromotionRoute.BASELINE
    await database.close()


@pytest.mark.asyncio
async def test_only_selected_can_start_and_kill_switch_is_audited(tmp_path: Path) -> None:
    database, registry, _, controller, campaign, _, proposals = await setup_campaign(tmp_path)
    unselected = next(item for item in proposals if item.proposal_id != campaign.proposal_id)
    with pytest.raises(DnaPromotionError, match="another request"):
        await controller.start(PromotionRequest(
            campaign.campaign_id, campaign.selection_id, unselected, "correlation",
        ))
    with pytest.raises(DnaPromotionError, match="only an H08 selected"):
        await controller.start(PromotionRequest(
            "campaign-unselected", campaign.selection_id, unselected, "correlation",
        ))
    with pytest.raises(DnaPromotionError, match="reason"):
        await controller.kill(campaign.campaign_id, reason="", correlation_id="correlation")
    with pytest.raises(DnaPromotionError, match="routing key"):
        await controller.route(campaign.campaign_id, "")
    with pytest.raises(DnaPromotionError, match="not found"):
        await controller.get("campaign-missing")
    stopped = await controller.kill(
        campaign.campaign_id, reason="operator emergency", correlation_id="correlation",
    )
    assert stopped.stage is PromotionStage.STOPPED
    assert (await registry.get(stopped.dna_id,
                               stopped.dna_version)).dna.status is DnaStatus.RETIRED
    assert await controller.kill(stopped.campaign_id, reason="again",
                                 correlation_id="correlation") == stopped
    rows = await database.fetch_all(
        "SELECT reason FROM dna_promotion_event WHERE campaign_id=? ORDER BY rowid",
        (campaign.campaign_id,),
    )
    assert str(rows[-1]["reason"]).startswith("kill_switch:")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_promotion_event WHERE campaign_id=?", (campaign.campaign_id,),
            )
    await database.close()


@pytest.mark.asyncio
async def test_threshold_failure_and_immediate_side_effect_stop(tmp_path: Path) -> None:
    threshold_path = tmp_path / "threshold"
    threshold_path.mkdir()
    database, _, clock, controller, campaign, _, _ = await setup_campaign(threshold_path)
    with pytest.raises(DnaPromotionError, match="predates"):
        await controller.observe(PromotionObservation(
            "old", campaign.campaign_id, True, True,
            campaign.stage_started_at - timedelta(seconds=1), "correlation",
        ))
    for ordinal in range(2):
        campaign = await controller.observe(PromotionObservation(
            f"failed-{ordinal}", campaign.campaign_id, False, True,
            clock.now(), "correlation",
        ))
    assert campaign.stage is PromotionStage.STOPPED
    with pytest.raises(DnaPromotionError, match="terminal"):
        await controller.observe(PromotionObservation(
            "terminal", campaign.campaign_id, True, True, clock.now(), "correlation",
        ))
    await database.close()


@pytest.mark.asyncio
async def test_manual_gate_evaluation_revision_and_rollback(tmp_path: Path) -> None:
    database, _, clock, controller, campaign, _, _ = await setup_campaign(tmp_path)
    with pytest.raises(DnaPromotionError, match="revision conflict"):
        await controller.evaluate(
            campaign.campaign_id, expected_revision=99, correlation_id="manual",
        )
    unchanged = await controller.evaluate(
        campaign.campaign_id, expected_revision=0, correlation_id="manual",
    )
    assert unchanged.stage is PromotionStage.SHADOW
    with pytest.raises(DnaPromotionError, match="only an active"):
        await controller.rollback(
            campaign.campaign_id, expected_revision=0, reason="manual",
            correlation_id="manual",
        )
    with pytest.raises(DnaPromotionError, match="rollback reason"):
        await controller.rollback(
            campaign.campaign_id, expected_revision=0, reason="", correlation_id="manual",
        )
    with pytest.raises(DnaPromotionError, match="revision conflict"):
        await controller.kill(
            campaign.campaign_id, expected_revision=1, reason="manual",
            correlation_id="manual",
        )
    for ordinal in range(2):
        campaign = await controller.observe(PromotionObservation(
            f"manual-shadow-{ordinal}", campaign.campaign_id, True, True,
            clock.now(), "manual",
        ))
    for ordinal in range(2):
        campaign = await controller.observe(PromotionObservation(
            f"manual-canary-{ordinal}", campaign.campaign_id, True, True,
            clock.now(), "manual",
        ))
    rolled_back = await controller.rollback(
        campaign.campaign_id, expected_revision=2, reason="operator choice",
        correlation_id="manual",
    )
    assert rolled_back.stage is PromotionStage.ROLLED_BACK
    await database.close()

    side_effect_path = tmp_path / "side-effect"
    side_effect_path.mkdir()
    database, _, clock, controller, campaign, _, _ = await setup_campaign(side_effect_path)
    campaign = await controller.observe(PromotionObservation(
        "duplicate-effect", campaign.campaign_id, True, True, clock.now(), "correlation",
        duplicate_side_effect=True,
    ))
    assert campaign.stage is PromotionStage.STOPPED
    await database.close()


@pytest.mark.asyncio
async def test_cli_promotion_gate_and_kill_use_campaign_cas(tmp_path: Path) -> None:
    database, _, _, _, campaign, _, _ = await setup_campaign(tmp_path)
    path = database._path
    await database.close()
    out, err = StringIO(), StringIO()
    code = await run((
        "--database", path, "evolution", "promote", campaign.campaign_id,
        "--revision", "0", "--reason", "operator gate", "--yes",
    ), out, err)
    assert code == 0 and json.loads(out.getvalue())["status"] == "SHADOW"
    out, err = StringIO(), StringIO()
    code = await run((
        "--database", path, "evolution", "kill", campaign.campaign_id,
        "--revision", "0", "--reason", "operator stop", "--yes",
    ), out, err)
    assert code == 0 and json.loads(out.getvalue())["status"] == "STOPPED"


def test_promotion_contracts() -> None:
    with pytest.raises(DnaPromotionError, match="samples"):
        PromotionPolicy("", 0, 0, timedelta(0), timedelta(0), 0.1)
    with pytest.raises(DnaPromotionError, match="rates"):
        PromotionPolicy("v1", 1, 1, timedelta(0), timedelta(0), 0)
    with pytest.raises(DnaPromotionError, match="durations"):
        PromotionPolicy("v1", 1, 1, timedelta(seconds=-1), timedelta(0), 0.1)
    with pytest.raises(DnaPromotionError, match="request metadata"):
        PromotionRequest("!", "", None, "")  # type: ignore[arg-type]
    with pytest.raises(DnaPromotionError, match="unique"):
        PromotionObservation("o", "c", True, True, START, "c", ("risk", "risk"))
    with pytest.raises(DnaPromotionError, match="observation metadata"):
        PromotionObservation("", "", True, True, START, "")
