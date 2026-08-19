from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_dna_candidates import START, mutation
from test_dna_candidates import setup as candidate_setup

from active_agent_platform.foundation import FakeClock
from domain_sdk import (
    CandidateOperation,
    CandidateOperationKind,
    DnaPopulationSelector,
    DnaReplayError,
    DnaSandboxReplay,
    DnaSelectionError,
    PopulationCandidate,
    ReplayContext,
    ReplayMeasurement,
    ReplayPolicy,
    ReplayRequest,
    SelectionDisposition,
    SelectionPolicy,
    SelectionRequest,
)


class PopulationExecutor:
    async def execute(self, dna, sample, context: ReplayContext) -> ReplayMeasurement:  # type: ignore[no-untyped-def]
        metrics = {
            "1.0.0": (0.8, 0.7, 10, 100, True, ()),
            "3.0.0": (0.9, 0.9, 8, 80, True, ()),
            "4.0.0": (1.0, 0.75, 9, 90, True, ()),
            "5.0.0": (1.0, 1.0, 7, 70, True, ("risk",)),
        }
        evidence, value, cost, latency, stable, risks = metrics[dna.version]
        digest = hashlib.sha256(
            f"{dna.content_digest}:{sample.sample_id}:{context.deterministic_seed}".encode()
        ).hexdigest()
        return ReplayMeasurement(True, evidence, value, cost, latency, stable, risks,
                                 f"sha256:{digest}")


async def setup_population(tmp_path: Path):  # type: ignore[no-untyped-def]
    database, base, _, dataset, generator = await candidate_setup(tmp_path)
    proposals = []
    for ordinal, (version, temperature) in enumerate(
        (("3.0.0", 0.1), ("4.0.0", 0.2), ("5.0.0", 0.3)), start=1,
    ):
        proposals.append(await generator.generate(mutation(
            base, dataset, proposal_id=f"population-proposal-{ordinal}", new_version=version,
            operations=(CandidateOperation(
                CandidateOperationKind.SET_INPUT, "summary", "temperature", temperature,
            ),),
        )))
    replay = DnaSandboxReplay(
        database, FakeClock(START), PopulationExecutor(),
        ReplayPolicy("population-replay/1.0", minimum_cases=4,
                     maximum_candidate_risk_rate=0),
    )
    reports = [await replay.run(ReplayRequest(
        f"population-replay-{ordinal}", proposal, base, dataset, "population-correlation",
    )) for ordinal, proposal in enumerate(proposals, start=1)]
    return database, proposals, reports


@pytest.mark.asyncio
async def test_population_selection_applies_hard_gate_pareto_diversity_and_capacity(
    tmp_path: Path,
) -> None:
    database, proposals, reports = await setup_population(tmp_path)
    selector = DnaPopulationSelector(
        database, FakeClock(START), SelectionPolicy("selection/1.0", maximum_survivors=1),
    )
    request = SelectionRequest(
        "selection-main", tuple(PopulationCandidate(proposal, report)
                                for proposal, report in zip(proposals, reports, strict=True)),
        "selection-correlation",
    )
    result = await selector.select(request)
    assert result.selected_proposal_ids == ("population-proposal-2",)
    dispositions = {item.proposal_id: item.disposition for item in result.members}
    assert dispositions == {
        "population-proposal-1": SelectionDisposition.CAPACITY,
        "population-proposal-2": SelectionDisposition.SELECTED,
        "population-proposal-3": SelectionDisposition.HARD_REJECTED,
    }
    assert result.members[0].pareto_rank == 0
    assert result.members[2].reasons[0] == "replay_failed"
    assert await selector.select(request) == result
    assert await selector.get(request.selection_id) == result
    with pytest.raises(DnaSelectionError, match="another request"):
        await selector.select(replace(request, candidates=tuple(reversed(request.candidates))))
    await database.close()


@pytest.mark.asyncio
async def test_population_selection_rejects_forgery_and_is_append_only(tmp_path: Path) -> None:
    database, proposals, reports = await setup_population(tmp_path)
    selector = DnaPopulationSelector(
        database, FakeClock(START), SelectionPolicy("selection/1.0", maximum_survivors=2),
    )
    candidates = tuple(PopulationCandidate(proposal, report)
                       for proposal, report in zip(proposals[:2], reports[:2], strict=True))
    strict_selector = DnaPopulationSelector(
        database, FakeClock(START), SelectionPolicy(
            "selection/strict", maximum_survivors=1, minimum_population=3,
        ),
    )
    with pytest.raises(DnaSelectionError, match="below minimum"):
        await strict_selector.select(SelectionRequest(
            "selection-small", candidates, "correlation",
        ))
    with pytest.raises(DnaSelectionError, match="IDs must be unique"):
        await selector.select(SelectionRequest(
            "selection-duplicate-id", (candidates[0], candidates[0]), "correlation",
        ))
    with pytest.raises(DnaSelectionError, match="Candidate proposal"):
        await selector.select(SelectionRequest(
            "selection-bad-proposal", (replace(
                candidates[0], proposal=replace(
                    candidates[0].proposal, proposal_digest="sha256:forged",
                ),
            ), candidates[1]), "correlation",
        ))
    with pytest.raises(DnaSelectionError, match="Replay report"):
        await selector.select(SelectionRequest(
            "selection-forged", (replace(candidates[0], replay=replace(
                candidates[0].replay, report_digest="sha256:forged")), candidates[1]),
            "correlation",
        ))
    result = await selector.select(SelectionRequest("selection-audit", candidates, "correlation"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_selection_run WHERE selection_id=?", (result.selection_id,),
            )
    with pytest.raises(DnaSelectionError, match="not found"):
        await selector.get("selection-missing")
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_selection_run_no_update")
        await transaction.execute(
            "UPDATE dna_selection_run SET report_digest='sha256:bad' WHERE selection_id=?",
            (result.selection_id,),
        )
    with pytest.raises(DnaSelectionError, match="report digest mismatch"):
        await selector.get(result.selection_id)
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_selection_run SET report_digest=? WHERE selection_id=?",
            (result.report_digest, result.selection_id),
        )
        await transaction.execute("DROP TRIGGER dna_selection_member_no_update")
        await transaction.execute(
            """UPDATE dna_selection_member SET member_digest='sha256:bad'
               WHERE selection_id=? AND proposal_id=?""",
            (result.selection_id, result.members[0].proposal_id),
        )
    with pytest.raises(DnaSelectionError, match="member digest mismatch"):
        await selector.get(result.selection_id)
    await database.close()


def test_selection_contracts() -> None:
    with pytest.raises(DnaSelectionError, match="counts"):
        SelectionPolicy("", 0)
    with pytest.raises(DnaSelectionError, match="novelty"):
        SelectionPolicy("v1", 1, minimum_novelty=2)
    with pytest.raises(DnaSelectionError, match="population is empty"):
        SelectionRequest("selection-empty", (), "correlation")
    with pytest.raises(DnaSelectionError, match="request metadata"):
        SelectionRequest("!", (), "")
    assert issubclass(DnaReplayError, ValueError)
