from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from test_dna_candidates import START, mutation
from test_dna_candidates import setup as candidate_setup

from active_agent_platform.foundation import FakeClock
from domain_sdk import (
    DatasetSplit,
    DnaReplayError,
    DnaSandboxReplay,
    FaultScenario,
    ReplayContext,
    ReplayMeasurement,
    ReplayPolicy,
    ReplayRequest,
    ReplayStatus,
)


class FakeSandboxExecutor:
    def __init__(self, *, nondeterministic_candidate: bool = False,
                 risky_candidate: bool = False,
                 nondeterministic_parent: bool = False) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.nondeterministic_candidate = nondeterministic_candidate
        self.risky_candidate = risky_candidate
        self.nondeterministic_parent = nondeterministic_parent

    async def execute(self, dna, sample, context: ReplayContext) -> ReplayMeasurement:  # type: ignore[no-untyped-def]
        key = (dna.content_digest, sample.sample_id)
        self.calls[key] += 1
        candidate = dna.version == "3.0.0"
        faulted = context.fault is not FaultScenario.NONE
        successful = candidate or not faulted
        sequence = self.calls[key] if (
            candidate and self.nondeterministic_candidate
            or not candidate and self.nondeterministic_parent
        ) else 0
        output = hashlib.sha256(
            f"{dna.content_digest}:{sample.sample_id}:{context.deterministic_seed}:{sequence}".encode()
        ).hexdigest()
        return ReplayMeasurement(
            successful, 1.0 if candidate else 0.8, 0.9 if candidate else 0.7,
            8 if candidate else 10, 80 if candidate else (200 if faulted else 100),
            candidate or not faulted,
            ("sandbox_risk",) if candidate and self.risky_candidate else (),
            f"sha256:{output}",
        )


async def setup(tmp_path: Path, executor: FakeSandboxExecutor, **policy_changes: object):  # type: ignore[no-untyped-def]
    database, base, _, dataset, generator = await candidate_setup(tmp_path)
    proposal = await generator.generate(mutation(base, dataset))
    values: dict[str, object] = {
        "policy_version": "replay-policy/1.0", "minimum_cases": 4,
        "minimum_success_delta": 0, "minimum_evidence_delta": 0,
        "minimum_value_delta": 0, "minimum_stability_delta": 0,
        "maximum_cost_increase_ratio": 0, "maximum_latency_increase_ratio": 0,
        "maximum_candidate_risk_rate": 0,
    }
    values.update(policy_changes)
    replay = DnaSandboxReplay(database, FakeClock(START), executor,
                              ReplayPolicy(**values))  # type: ignore[arg-type]
    return database, base, dataset, proposal, replay


@pytest.mark.asyncio
async def test_historical_replay_compares_same_cases_seed_and_faults(tmp_path: Path) -> None:
    executor = FakeSandboxExecutor()
    database, base, dataset, proposal, replay = await setup(tmp_path, executor)
    selected = [item for item in dataset.samples
                if item.split in {DatasetSplit.VALIDATION, DatasetSplit.TEST}]
    request = ReplayRequest(
        "replay-pass-1", proposal, base, dataset, "replay-correlation",
        {selected[0].sample_id: FaultScenario.TIMEOUT,
         selected[1].sample_id: FaultScenario.SKILL_FAILURE},
    )
    report = await replay.run(request)
    assert report.status is ReplayStatus.PASSED and report.reasons == ()
    assert len(report.cases) == 4
    assert report.candidate.success_rate > report.parent.success_rate
    assert report.candidate.average_cost_minor < report.parent.average_cost_minor
    assert report.candidate.average_latency_ms < report.parent.average_latency_ms
    assert report.cases[0].context.virtual_time == selected[0].observed_at
    assert report.cases[0].context.fault is FaultScenario.TIMEOUT
    assert report.cases[0].parent_deterministic
    calls = sum(executor.calls.values())
    assert await replay.run(request) == report
    assert sum(executor.calls.values()) == calls
    assert await replay.get(request.replay_id) == report
    await database.close()


@pytest.mark.asyncio
async def test_nondeterminism_and_risk_are_hard_failures(tmp_path: Path) -> None:
    executor = FakeSandboxExecutor(
        nondeterministic_candidate=True, risky_candidate=True,
        nondeterministic_parent=True,
    )
    database, base, dataset, proposal, replay = await setup(tmp_path, executor)
    report = await replay.run(ReplayRequest(
        "replay-fail-1", proposal, base, dataset, "replay-correlation"
    ))
    assert report.status is ReplayStatus.FAILED
    assert "candidate_nondeterministic" in report.reasons
    assert "parent_nondeterministic" in report.reasons
    assert "candidate_risk_exceeded" in report.reasons
    assert report.candidate.risk_rate == 1
    await database.close()


@pytest.mark.asyncio
async def test_replay_rejects_training_leakage_forgery_and_conflicting_id(tmp_path: Path) -> None:
    executor = FakeSandboxExecutor()
    database, base, dataset, proposal, replay = await setup(tmp_path, executor)
    training = next(item for item in dataset.samples if item.split is DatasetSplit.TRAIN)
    with pytest.raises(DnaReplayError, match="unselected sample"):
        await replay.run(ReplayRequest(
            "replay-bad-fault", proposal, base, dataset, "correlation",
            {training.sample_id: FaultScenario.TIMEOUT},
        ))
    forged = replace(dataset, manifest=replace(dataset.manifest,
                                               manifest_digest="sha256:forged"))
    with pytest.raises(DnaReplayError, match="proposal is missing or does not match"):
        await replay.run(ReplayRequest(
            "replay-forged", proposal, base, forged, "correlation"
        ))
    request = ReplayRequest("replay-stable-id", proposal, base, dataset, "correlation")
    await replay.run(request)
    with pytest.raises(DnaReplayError, match="another request"):
        await replay.run(replace(request, faults={dataset.samples[-1].sample_id:
                                                  FaultScenario.CANCELLED}))
    with pytest.raises(DnaReplayError, match="not found"):
        await replay.get("missing-replay")
    await database.close()


@pytest.mark.asyncio
async def test_replay_records_are_append_only_and_case_tampering_is_detected(tmp_path: Path) -> None:
    executor = FakeSandboxExecutor()
    database, base, dataset, proposal, replay = await setup(tmp_path, executor)
    request = ReplayRequest("replay-audit-1", proposal, base, dataset, "correlation")
    report = await replay.run(request)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute("DELETE FROM dna_replay_run WHERE replay_id=?",
                                      (report.replay_id,))
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_replay_case_no_update")
        await transaction.execute(
            "UPDATE dna_replay_case SET case_digest='sha256:bad' WHERE replay_id=?",
            (report.replay_id,),
        )
    with pytest.raises(DnaReplayError, match="case digest mismatch"):
        await replay.get(report.replay_id)
    await database.close()


@pytest.mark.asyncio
async def test_replay_rejects_insufficient_cases_and_persisted_source_tampering(
    tmp_path: Path,
) -> None:
    executor = FakeSandboxExecutor()
    for name in ("minimum", "parent", "missing-parent", "sample", "dataset", "threshold"):
        (tmp_path / name).mkdir()
    database, base, dataset, proposal, replay = await setup(
        tmp_path / "minimum", executor, minimum_cases=5,
    )
    with pytest.raises(DnaReplayError, match="insufficient"):
        await replay.run(ReplayRequest(
            "replay-too-small", proposal, base, dataset, "correlation",
        ))
    await database.close()

    database, base, dataset, proposal, replay = await setup(
        tmp_path / "parent", executor,
    )
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET document_json='{}' WHERE dna_id=? AND version=?",
            (base.dna_id, base.version),
        )
    with pytest.raises(DnaReplayError, match="parent DNA"):
        await replay.run(ReplayRequest(
            "replay-bad-parent", proposal, base, dataset, "correlation",
        ))
    await database.close()

    database, base, dataset, proposal, replay = await setup(
        tmp_path / "missing-parent", executor,
    )
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET content_digest='sha256:detached' "
            "WHERE dna_id=? AND version=?",
            (base.dna_id, base.version),
        )
    with pytest.raises(DnaReplayError, match="parent DNA"):
        await replay.run(ReplayRequest(
            "replay-missing-parent", proposal, base, dataset, "correlation",
        ))
    await database.close()

    database, base, dataset, proposal, replay = await setup(
        tmp_path / "sample", executor,
    )
    selected = next(item for item in dataset.samples
                    if item.split in {DatasetSplit.VALIDATION, DatasetSplit.TEST})
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_experience_sample_no_update")
        await transaction.execute(
            """UPDATE dna_experience_sample SET sample_digest='sha256:bad'
               WHERE dataset_id=? AND dataset_version=? AND sample_id=?""",
            (dataset.manifest.dataset_id, dataset.manifest.version, selected.sample_id),
        )
    with pytest.raises(DnaReplayError, match="sample is missing or digest"):
        await replay.run(ReplayRequest(
            "replay-bad-sample", proposal, base, dataset, "correlation",
        ))
    await database.close()

    database, base, dataset, proposal, replay = await setup(
        tmp_path / "dataset", executor,
    )
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_experience_dataset_no_update")
        await transaction.execute(
            """UPDATE dna_experience_dataset SET manifest_digest='sha256:bad'
               WHERE dataset_id=? AND version=?""",
            (dataset.manifest.dataset_id, dataset.manifest.version),
        )
    with pytest.raises(DnaReplayError, match="dataset is missing or manifest"):
        await replay.run(ReplayRequest(
            "replay-bad-dataset", proposal, base, dataset, "correlation",
        ))
    await database.close()

    database, base, dataset, proposal, replay = await setup(
        tmp_path / "threshold", executor,
        minimum_success_delta=0.1, minimum_evidence_delta=0.3,
        minimum_value_delta=0.3, minimum_stability_delta=0.1,
    )
    report = await replay.run(ReplayRequest(
        "replay-thresholds", proposal, base, dataset, "correlation",
    ))
    assert report.status is ReplayStatus.FAILED
    assert set(report.reasons) >= {
        "success_delta_below_threshold", "evidence_delta_below_threshold",
        "value_delta_below_threshold", "stability_delta_below_threshold",
    }
    await database.close()


@pytest.mark.asyncio
async def test_replay_report_tampering_is_detected(tmp_path: Path) -> None:
    executor = FakeSandboxExecutor()
    database, base, dataset, proposal, replay = await setup(tmp_path, executor)
    report = await replay.run(ReplayRequest(
        "replay-report-tamper", proposal, base, dataset, "correlation",
    ))
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_replay_run_no_update")
        await transaction.execute(
            "UPDATE dna_replay_run SET report_digest='sha256:bad' WHERE replay_id=?",
            (report.replay_id,),
        )
    with pytest.raises(DnaReplayError, match="report digest mismatch"):
        await replay.get(report.replay_id)
    await database.close()

    missing_case_path = tmp_path / "missing-case"
    missing_case_path.mkdir()
    database, base, dataset, proposal, replay = await setup(missing_case_path, executor)
    report = await replay.run(ReplayRequest(
        "replay-case-missing", proposal, base, dataset, "correlation",
    ))
    async with database.transaction() as transaction:
        await transaction.execute("DROP TRIGGER dna_replay_case_no_delete")
        await transaction.execute(
            "DELETE FROM dna_replay_case WHERE replay_id=? AND ordinal=0",
            (report.replay_id,),
        )
    with pytest.raises(DnaReplayError, match="case digest mismatch"):
        await replay.get(report.replay_id)
    await database.close()


def test_replay_contracts_reject_invalid_values() -> None:
    digest = "sha256:" + "0" * 64
    with pytest.raises(DnaReplayError, match="scores"):
        ReplayMeasurement(True, 2, 1, 0, 0, True, (), digest)
    with pytest.raises(DnaReplayError, match="non-negative"):
        ReplayMeasurement(True, 1, 1, -1, 0, True, (), digest)
    with pytest.raises(DnaReplayError, match="unique"):
        ReplayMeasurement(True, 1, 1, 0, 0, True, ("x", "x"), digest)
    with pytest.raises(DnaReplayError, match="output digest"):
        ReplayMeasurement(True, 1, 1, 0, 0, True, (), "bad")
    with pytest.raises(DnaReplayError, match="counts"):
        ReplayPolicy("", repetitions=1)
    with pytest.raises(DnaReplayError, match="training split"):
        ReplayPolicy("v1", included_splits=frozenset({DatasetSplit.TRAIN}))
    with pytest.raises(DnaReplayError, match="ratios"):
        ReplayPolicy("v1", maximum_candidate_risk_rate=2)
    with pytest.raises(DnaReplayError, match="request metadata"):
        ReplayRequest("!", None, None, None, "")  # type: ignore[arg-type]
