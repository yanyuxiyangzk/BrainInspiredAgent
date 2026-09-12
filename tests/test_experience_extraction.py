"""X-02 tests: episodic outcome/trace → candidate experience extraction.

抽取器 ``domain_sdk.experience_extraction.ExperienceExtractor`` 把一次情景记忆
（Episode + OutcomeEvaluation + G01 TraceBundle）确定性地转成带证据链的
CANDIDATE 经验样本：

* golden：statement/summary 为固定文案模板，experience_id/content_digest 由
  输入内容与抽取器版本决定（golden 公式 + 固定文案双锚定），与时钟无关；
* 证据链：Episode/Outcome/Trace 三方引用必须互洽，缺一拒绝；
* 互操作：``to_document()`` 可直接作为 RestRepair ``complete()`` 的候选经验落库。
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import RepairOutcome, RestRepair
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.experience_extraction import (
    EvidenceChain,
    ExperienceCandidate,
    ExperienceExtractionError,
    ExperienceExtractor,
)
from domain_sdk.workflow_patch import digest_document

NOW = datetime(2026, 9, 12, 8, tzinfo=UTC)
DAY = date(2026, 9, 12)


def episode_document() -> dict[str, object]:
    return {
        "episode_id": "episode-1", "task_id": "task-1",
        "correlation_id": "correlation-1", "kind": "TASK_OUTCOME",
        "evidence": ["evidence-1"],
    }


def evaluation_document(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "evaluation_id": "evaluation-1", "episode_id": "episode-1",
        "task_id": "task-1", "correlation_id": "correlation-1",
        "evaluated_at": "2026-09-12T08:00:00Z", "evaluator_version": "rules/1",
        "task_status": "SUCCEEDED", "goal_id": "goal-1",
        "evidence_ids": ["evidence-1", "evidence-2"], "successful": True,
        "execution": {"status": "PASSED", "score": 1.0, "reasons": ["ok"]},
        "goal": {"status": "PASSED", "score": 0.9, "reasons": ["ok"]},
        "quality": {"status": "PASSED", "score": 0.8, "reasons": ["ok"]},
        "evidence": {"status": "PASSED", "score": 1.0, "reasons": ["ok"]},
    }
    document.update(changes)
    return document


def trace_document() -> dict[str, object]:
    return {
        "plans": [], "decisions": [], "grants": [],
        "tasks": [{"task_id": "task-1", "correlation_id": "correlation-1"}],
        "workflow_runs": [], "node_runs": [],
        "episodes": [{"episode_id": "episode-1"}], "audits": [],
    }


def extract(clock: FakeClock | None = None) -> ExperienceCandidate:
    return ExperienceExtractor(clock or FakeClock(NOW)).extract(
        episode=episode_document(), evaluation=evaluation_document(),
        trace=trace_document(),
    )


# ---------------------------------------------------------------------------
# golden：确定性产出与证据链
# ---------------------------------------------------------------------------


def test_extracts_candidate_with_golden_text_and_identity() -> None:
    candidate = extract()
    outcome = {
        "successful": True, "task_status": "SUCCEEDED",
        "execution": evaluation_document()["execution"],
        "goal": evaluation_document()["goal"],
        "quality": evaluation_document()["quality"],
        "evidence": evaluation_document()["evidence"],
    }
    assert candidate.experience_id == digest_document({
        "extractor": "experience-extractor/1.0",
        "episode_id": "episode-1", "evaluation_id": "evaluation-1",
        "correlation_id": "correlation-1", "outcome": outcome,
    })
    assert candidate.statement == (
        "Episode episode-1 finished SUCCEEDED: quality 0.80, "
        "execution 1.00, goal 0.90, evidence 1.00."
    )
    assert candidate.summary == "Successful task with quality 0.80 and 2 evidence items."
    assert candidate.successful is True
    assert candidate.quality_score == 0.8
    assert candidate.conditions == {"task_status": "SUCCEEDED", "goal_id": "goal-1"}
    assert candidate.evidence == EvidenceChain(
        episode_id="episode-1", evaluation_id="evaluation-1",
        correlation_id="correlation-1", task_status="SUCCEEDED",
        evidence_ids=("evidence-1", "evidence-2"),
        trace_digest=digest_document(trace_document()),
    )


def test_content_digest_excludes_extraction_time_and_round_trips() -> None:
    candidate = extract()
    document = candidate.to_document()
    assert document["status"] == "CANDIDATE"
    assert document["evidence_episode_ids"] == ["episode-1"]
    assert document["content_digest"] == digest_document({
        key: value for key, value in document.items()
        if key not in {"content_digest", "extracted_at"}
    })
    assert ExperienceCandidate.from_document(document) == candidate
    document["statement"] = "tampered"
    with pytest.raises(ExperienceExtractionError, match="digest"):
        ExperienceCandidate.from_document(document)


def test_extraction_is_idempotent_across_clocks() -> None:
    first = extract(FakeClock(NOW))
    second = extract(FakeClock(NOW.replace(hour=20)))
    assert second.extracted_at != first.extracted_at
    assert second.experience_id == first.experience_id
    assert second.content_digest == first.content_digest
    assert second.statement == first.statement


def test_failed_outcome_produces_candidate_with_failed_text() -> None:
    candidate = ExperienceExtractor(FakeClock(NOW)).extract(
        episode=episode_document(),
        evaluation=evaluation_document(
            successful=False, task_status="FAILED",
            execution={"status": "FAILED", "score": 0.2, "reasons": ["timeout"]},
        ),
        trace=trace_document(),
    )
    assert candidate.successful is False
    assert candidate.statement == (
        "Episode episode-1 finished FAILED: quality 0.80, "
        "execution 0.20, goal 0.90, evidence 1.00."
    )
    assert candidate.summary == "Failed task with quality 0.80 and 2 evidence items."


# ---------------------------------------------------------------------------
# 拒绝矩阵：引用互洽与证据完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ("episode_id_mismatch", "episode_id"),
        ("correlation_mismatch", "correlation"),
        ("non_terminal", "terminal"),
        ("missing_quality", "quality"),
        ("score_out_of_range", "score"),
        ("successful_not_bool", "successful"),
        ("trace_missing_episode", "trace bundle is missing episode"),
        ("trace_missing_task", "trace bundle is missing task"),
        ("empty_episode_id", "identity"),
    ],
)
def test_extraction_rejects_broken_evidence_chains(
    mutation: str, fragment: str,
) -> None:
    episode = episode_document()
    evaluation = evaluation_document()
    trace = trace_document()
    if mutation == "episode_id_mismatch":
        evaluation["episode_id"] = "episode-other"
    elif mutation == "correlation_mismatch":
        evaluation["correlation_id"] = "correlation-other"
    elif mutation == "non_terminal":
        evaluation["task_status"] = "PENDING"
    elif mutation == "missing_quality":
        del evaluation["quality"]
    elif mutation == "score_out_of_range":
        evaluation["quality"] = {"status": "PASSED", "score": 1.5, "reasons": []}
    elif mutation == "successful_not_bool":
        evaluation["successful"] = "yes"
    elif mutation == "trace_missing_episode":
        trace["episodes"] = []
    elif mutation == "trace_missing_task":
        trace["tasks"] = []
    elif mutation == "empty_episode_id":
        episode["episode_id"] = ""
    with pytest.raises(ExperienceExtractionError, match=fragment):
        ExperienceExtractor(FakeClock(NOW)).extract(
            episode=episode, evaluation=evaluation, trace=trace,
        )


# ---------------------------------------------------------------------------
# 互操作：候选经验可直接进入 RestRepair 日复盘落库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extracted_candidate_is_accepted_by_rest_repair(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "interop.db")
    await database.initialize()
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, ?)", (stamp, stamp, "correlation-1"))
        await tx.execute("INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, ?)", (stamp, "correlation-1"))
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', 'task-1', '{}', 'ACTIVE', ?, ?, ?)",
            (stamp, stamp, "correlation-1"),
        )
        await tx.execute(
            """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                 finished_at,deadline,correlation_id)
               VALUES ('task-1','grant','SUCCEEDED',1,1,?,?,?,?)""",
            (stamp, stamp, stamp, "correlation-1"),
        )
        await tx.execute(
            "INSERT INTO episode VALUES (?, 'task-1', '{}', ?, 'correlation-1')",
            ("episode-1", stamp),
        )

    candidate = extract()
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(
        UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(300, 380)
    ))
    decision = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert decision.outcome is RepairOutcome.REQUESTED
    assert decision.request is not None
    await repair.complete(
        decision.request.run_id, result={"note": "x"},
        candidate_experiences=(candidate.to_document(),),
    )
    row = await database.fetch_one(
        "SELECT status,result_json FROM rest_repair_run WHERE run_id = ?",
        (decision.request.run_id,),
    )
    assert row is not None and str(row["status"]) == "SUCCEEDED"
    stored = json.loads(str(row["result_json"]))
    assert stored["candidate_experiences"][0]["experience_id"] == candidate.experience_id
    await database.close()
