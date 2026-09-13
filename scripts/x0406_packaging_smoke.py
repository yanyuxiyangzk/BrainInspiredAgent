"""X-04/X-06 packaging smoke: offline evaluation set + memory A/B comparison."""
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.semantic_memory import SemanticCandidate, SemanticMemoryService
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.decision_ab import (
    AbPolicy,
    Decision,
    DecisionCase,
    DecisionQuality,
    run_ab_comparison,
)
from domain_sdk.semantic_evaluation import (
    DirectMatchRetriever,
    EvaluationPolicy,
    SemanticEvaluationSet,
    run_offline_evaluation,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def candidate(index: int, value: str) -> SemanticCandidate:
    return SemanticCandidate(
        claim_key=f"preferred.window.{index + 1}", claim_value=value,
        statement=f"preferred window is {value}",
        summary=f"window {value}",
        evidence_episode_ids=(f"episode-{index + 1}",),
        scope={"domain": "sample"}, conditions={"task_status": "SUCCEEDED"},
        confidence=0.9, data_version="data/1",
        valid_until=NOW + timedelta(hours=1), correlation_id=f"correlation-{index + 1}",
    )


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="x0406-smoke-"))
    database = SQLiteDatabase(workdir / "smoke.db")
    await database.initialize()
    clock = FakeClock(NOW)
    service = SemanticMemoryService(
        database, clock, FakeUuidGenerator(UUID(int=i) for i in range(1, 40))
    )
    async with database.transaction() as tx:
        for index in range(2):
            tag = str(index + 1)
            correlation = f"correlation-{tag}"
            await tx.execute(
                "INSERT INTO plan VALUES (?,?,?,?,?,?,?)",
                (f"plan-{tag}", "{}", f"digest-{tag}", "CANDIDATE",
                 NOW.isoformat().replace("+00:00", "Z"),
                 NOW.isoformat().replace("+00:00", "Z"), correlation),
            )
            await tx.execute(
                "INSERT INTO plan_decision VALUES (?,?,?,?,?,?)",
                (f"decision-{tag}", f"plan-{tag}", "APPROVED", "{}",
                 NOW.isoformat().replace("+00:00", "Z"), correlation),
            )
            await tx.execute(
                "INSERT INTO execution_grant VALUES (?,?,?,?,?,?,?,?)",
                (f"grant-{tag}", f"decision-{tag}", f"task-{tag}", "{}", "ACTIVE",
                 NOW.isoformat().replace("+00:00", "Z"),
                 NOW.isoformat().replace("+00:00", "Z"), correlation),
            )
            await tx.execute(
                """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                     finished_at,deadline,correlation_id)
                   VALUES (?,?,'SUCCEEDED',1,1,?,?,?,?)""",
                (f"task-{tag}", f"grant-{tag}",
                 NOW.isoformat().replace("+00:00", "Z"),
                 NOW.isoformat().replace("+00:00", "Z"),
                 NOW.isoformat().replace("+00:00", "Z"), correlation),
            )
            await tx.execute(
                "INSERT INTO episode VALUES (?,?,?,?,?)",
                (f"episode-{tag}", f"task-{tag}", "{}",
                 NOW.isoformat().replace("+00:00", "Z"), correlation),
            )

    for index, value in enumerate(("5d", "10d")):
        record = await service.propose(candidate(index, value))
        promotion = await service.promote(
            record.memory_id, validation_method="x03-replay"
        )
        assert promotion.promoted, promotion.reason

    corpus = await service.validated()
    assert len(corpus) == 2
    evaluation_set = SemanticEvaluationSet.build_from_corpus(
        corpus, forbidden_ids=frozenset({"m-polluted"})
    )
    report = await run_offline_evaluation(
        evaluation_set, DirectMatchRetriever(corpus), EvaluationPolicy(),
        correlation_id="x04-smoke",
    )
    assert report.status == "PASSED" and report.metrics["recall_rate"] == 1.0

    cases = [
        DecisionCase(f"case-{index}", {"window": "10d"})
        for index in range(4)
    ]

    class Baseline:
        async def decide(self, case: DecisionCase) -> Decision:
            return Decision({"window": "1d"})

    class MemoryArm:
        async def decide(self, case: DecisionCase) -> Decision:
            return Decision({"window": "10d"}, ("m-validated",))

    class Judge:
        def assess(self, case: DecisionCase, decision: Decision) -> DecisionQuality:
            return DecisionQuality(
                decision.document["window"] == "10d",
                1.0 if decision.document["window"] == "10d" else 0.2,
            )

    ab = await run_ab_comparison(
        cases, Baseline(), MemoryArm(), Judge(),
        forbidden_memory_ids=frozenset({"m-polluted"}),
        policy=AbPolicy(), correlation_id="x06-smoke",
    )
    assert ab.status == "PASSED" and ab.metrics["quality_delta"] == 0.8, ab.metrics
    print("X-04/X-06 packaging smoke PASS:",
          json.dumps({"recall": report.metrics["recall_rate"],
                      "quality_delta": ab.metrics["quality_delta"]}))
    return 0


raise SystemExit(asyncio.run(main()))
