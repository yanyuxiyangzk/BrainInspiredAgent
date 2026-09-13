"""X-04 tests: semantic-memory evaluation set and error-recall metrics.

评估集从真实语义记忆语料（`SemanticMemoryService` 的 VALIDATED 记录）确定性
构建；`DirectMatchRetriever` 是内置的确定性检索器（向量库按路线图延后）。
离线报告度量的"错误召回"= 检索命中了被矛盾/拒绝的记忆。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.semantic_memory import (
    SemanticCandidate,
    SemanticMemoryService,
)
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.semantic_evaluation import (
    DirectMatchRetriever,
    EvaluationCase,
    EvaluationPolicy,
    OfflineEvaluationReport,
    SemanticEvaluationSet,
    run_offline_evaluation,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def candidate(claim_key: str, claim_value: object, *, scope: dict[str, object] | None = None,
              episode_id: str = "episode-1", confidence: float = 0.9) -> SemanticCandidate:
    return SemanticCandidate(
        claim_key=claim_key, claim_value=claim_value,
        statement=f"{claim_key} is {claim_value}",
        summary=f"{claim_key} summary",
        evidence_episode_ids=(episode_id,),
        scope=scope or {"domain": "sample"},
        conditions={"task_status": "SUCCEEDED"},
        confidence=confidence, data_version="data/1",
        valid_until=NOW + timedelta(hours=1), correlation_id=f"correlation-{claim_key}",
    )


async def seed_corpus(tmp_path: Path) -> tuple[SQLiteDatabase, SemanticMemoryService]:
    database = SQLiteDatabase(tmp_path / "semantic.db")
    await database.initialize()
    clock = FakeClock(NOW)
    service = SemanticMemoryService(
        database, clock, FakeUuidGenerator(UUID(int=i) for i in range(1, 40))
    )
    async with database.transaction() as tx:
        for index in range(3):
            tag = str(index + 1)
            stamp = NOW.isoformat().replace("+00:00", "Z")
            correlation = f"correlation-{tag}"
            await tx.execute(
                "INSERT INTO plan VALUES (?,?,?,?,?,?,?)",
                (f"plan-{tag}", json.dumps({"plan": tag}), f"digest-{tag}",
                 "CANDIDATE", stamp, stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO plan_decision VALUES (?,?,?,?,?,?)",
                (f"decision-{tag}", f"plan-{tag}", "APPROVED",
                 json.dumps({"decision": "APPROVED"}), stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO execution_grant VALUES (?,?,?,?,?,?,?,?)",
                (f"grant-{tag}", f"decision-{tag}", f"task-{tag}",
                 json.dumps({"grant": tag}), "ACTIVE", stamp, stamp, correlation),
            )
            await tx.execute(
                """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                     finished_at,deadline,correlation_id)
                   VALUES (?,?,'SUCCEEDED',1,1,?,?,?,?)""",
                (f"task-{tag}", f"grant-{tag}", stamp, stamp, stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO episode VALUES (?,?,?,?,?)",
                (f"episode-{tag}", f"task-{tag}",
                 json.dumps({"evidence": [f"evidence-{tag}"]}), stamp, correlation),
            )
    return database, service


async def promote_validated(service: SemanticMemoryService, spec: SemanticCandidate) -> str:
    record = await service.propose(spec)
    result = await service.promote(record.memory_id, validation_method="x03-replay")
    assert result.promoted
    return record.memory_id


# --------------------------------------------------------------------------- 评估集


@pytest.mark.asyncio
async def test_evaluation_set_builds_from_real_corpus(tmp_path: Path) -> None:
    database, service = await seed_corpus(tmp_path)
    try:
        validated_ids = [
            await promote_validated(service, candidate(
                f"preferred.window.{index + 1}", value, episode_id=f"episode-{index + 1}",
            ))
            for index, value in enumerate(("5d", "10d", "20d"))
        ]
        corpus = await service.validated()
        assert len(corpus) == 3
        evaluation_set = SemanticEvaluationSet.build_from_corpus(
            corpus, forbidden_ids=frozenset({"bad-memory"})
        )
        assert len(evaluation_set.cases) == 3
        assert len({case.query["claim_key"] for case in evaluation_set.cases}) == 3
        for case in evaluation_set.cases:
            assert len(case.expected_memory_ids) == 1
            assert case.forbidden_memory_ids == frozenset({"bad-memory"})
        # 文档 round-trip 且防篡改
        document = evaluation_set.to_document()
        restored = SemanticEvaluationSet.from_document(document)
        assert restored == evaluation_set
        broken = dict(document)
        broken["cases"] = document["cases"][:-1]
        with pytest.raises(ValueError, match="digest"):
            SemanticEvaluationSet.from_document(broken)
        assert set(validated_ids) == {next(iter(c.expected_memory_ids)) for c in evaluation_set.cases}
    finally:
        await database.close()


def test_case_and_policy_validate_bounds() -> None:
    with pytest.raises(ValueError, match="expected"):
        EvaluationCase({"claim_key": "k"}, frozenset(), frozenset())
    with pytest.raises(ValueError, match="overlap"):
        EvaluationCase(
            {"claim_key": "k"}, frozenset({"m1"}),
            frozenset({"m1"}),
        )
    with pytest.raises(ValueError, match="recall"):
        EvaluationPolicy(min_recall_rate=-0.1)
    with pytest.raises(ValueError, match="error"):
        EvaluationPolicy(max_error_recall_rate=1.5)
    with pytest.raises(ValueError, match="cases"):
        EvaluationPolicy(min_cases=0)


# --------------------------------------------------------------------------- 离线评估


@pytest.mark.asyncio
async def test_golden_offline_report_passes(tmp_path: Path) -> None:
    database, service = await seed_corpus(tmp_path)
    try:
        for index, value in enumerate(("5d", "10d")):
            await promote_validated(service, candidate(
                f"preferred.window.{index + 1}", value, episode_id=f"episode-{index + 1}",
            ))
        corpus = await service.validated()
        evaluation_set = SemanticEvaluationSet.build_from_corpus(
            corpus, forbidden_ids=frozenset({"m-bad"})
        )
        retriever = DirectMatchRetriever(corpus)
        report = await run_offline_evaluation(
            evaluation_set, retriever, EvaluationPolicy(), correlation_id="offline-1",
        )
        assert report.status == "PASSED"
        assert report.metrics["recall_rate"] == 1.0
        assert report.metrics["error_recall_rate"] == 0.0
        assert report.metrics["case_pass_rate"] == 1.0
        assert len(report.case_outcomes) == 2
        assert all(case["passed"] for case in report.case_outcomes)
        # 报告 round-trip
        restored = OfflineEvaluationReport.from_document(report.to_document())
        assert restored == report
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_missing_memory_fails_recall_floor(tmp_path: Path) -> None:
    database, service = await seed_corpus(tmp_path)
    try:
        for index, value in enumerate(("5d", "10d")):
            await promote_validated(service, candidate(
                f"preferred.window.{index + 1}", value, episode_id=f"episode-{index + 1}",
            ))
        corpus = await service.validated()
        evaluation_set = SemanticEvaluationSet.build_from_corpus(
            corpus, forbidden_ids=frozenset()
        )
        degraded = DirectMatchRetriever(corpus[:1])  # 一半记忆丢失
        report = await run_offline_evaluation(
            evaluation_set, degraded, EvaluationPolicy(), correlation_id="offline-2",
        )
        assert report.status == "FAILED"
        assert report.metrics["recall_rate"] < 1.0
        assert report.failure_reasons
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_forbidden_recall_fails_error_budget(tmp_path: Path) -> None:
    database, service = await seed_corpus(tmp_path)
    try:
        await promote_validated(service, candidate("preferred.window", "5d"))
        corpus = await service.validated()
        evaluation_set = SemanticEvaluationSet.build_from_corpus(
            corpus, forbidden_ids=frozenset({"m-bad"})
        )

        class PollutedRetriever:
            async def recall(self, query: dict[str, object]) -> tuple[str, ...]:
                expected = next(
                    case.expected_memory_ids for case in evaluation_set.cases
                    if case.query == query
                )
                return (*expected, "m-bad")

        report = await run_offline_evaluation(
            evaluation_set, PollutedRetriever(), EvaluationPolicy(), correlation_id="offline-3",
        )
        assert report.status == "FAILED"
        assert report.metrics["error_recall_rate"] == 1.0
        assert any("error" in reason for reason in report.failure_reasons)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_min_cases_guard(tmp_path: Path) -> None:
    database, service = await seed_corpus(tmp_path)
    try:
        await promote_validated(service, candidate("preferred.window", "5d"))
        corpus = await service.validated()
        evaluation_set = SemanticEvaluationSet.build_from_corpus(
            corpus, forbidden_ids=frozenset()
        )
        report = await run_offline_evaluation(
            evaluation_set, DirectMatchRetriever(corpus),
            EvaluationPolicy(min_cases=5), correlation_id="offline-4",
        )
        assert report.status == "FAILED"
        assert any("cases" in reason for reason in report.failure_reasons)
    finally:
        await database.close()


def test_direct_match_retriever_honours_scope() -> None:
    corpus_records = []  # 以真实记录驱动：scope 不同则不命中
    from active_agent_platform.semantic_memory import SemanticMemoryRecord, SemanticStatus

    for index, (value, scope) in enumerate((
        ("5d", {"domain": "sample"}), ("10d", {"domain": "other"}),
    )):
        corpus_records.append(SemanticMemoryRecord(
            f"m-{index}", candidate("preferred.window", value, scope=scope),
            SemanticStatus.VALIDATED, "test", (), NOW, NOW,
        ))

    async def scenario() -> tuple[int, int]:
        retriever = DirectMatchRetriever(tuple(corpus_records))
        same = await retriever.recall({"claim_key": "preferred.window", "scope": {"domain": "sample"}})
        other = await retriever.recall({"claim_key": "preferred.window", "scope": {"domain": "other"}})
        return len(same), len(other)

    import asyncio

    same_count, other_count = asyncio.run(scenario())
    assert same_count == 1 and other_count == 1
