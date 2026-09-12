"""X-02 packaging smoke: extract a candidate experience via the installed wheel.

在全新 venv（仅安装 bia wheel）中验证 X-02 抽取器：Episode/Outcome/Trace →
CANDIDATE 经验（证据链互洽、幂等），并把 ``to_document()`` 直接交给
``RestRepair.complete`` 落库。不导入 tests/。

用法：python scripts/x02_packaging_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from active_agent_platform import RepairOutcome, RestRepair
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import ExperienceExtractor

NOW = datetime(2026, 9, 12, 10, tzinfo=UTC)
DAY = date(2026, 9, 12)

EPISODE = {
    "episode_id": "episode-1", "task_id": "task-1",
    "correlation_id": "correlation-1", "kind": "TASK_OUTCOME",
    "evidence": ["evidence-1"],
}
EVALUATION = {
    "evaluation_id": "evaluation-1", "episode_id": "episode-1",
    "task_id": "task-1", "correlation_id": "correlation-1",
    "evaluated_at": "2026-09-12T10:00:00Z", "evaluator_version": "rules/1",
    "task_status": "SUCCEEDED", "goal_id": "goal-1",
    "evidence_ids": ["evidence-1", "evidence-2"], "successful": True,
    "execution": {"status": "PASSED", "score": 1.0, "reasons": ["ok"]},
    "goal": {"status": "PASSED", "score": 0.9, "reasons": ["ok"]},
    "quality": {"status": "PASSED", "score": 0.8, "reasons": ["ok"]},
    "evidence": {"status": "PASSED", "score": 1.0, "reasons": ["ok"]},
}
TRACE = {
    "plans": [], "decisions": [], "grants": [],
    "tasks": [{"task_id": "task-1", "correlation_id": "correlation-1"}],
    "workflow_runs": [], "node_runs": [],
    "episodes": [{"episode_id": "episode-1"}], "audits": [],
}


async def main() -> int:
    extractor = ExperienceExtractor(FakeClock(NOW))
    candidate = extractor.extract(
        episode=EPISODE, evaluation=EVALUATION, trace=TRACE,
    )
    replay = extractor.extract(episode=EPISODE, evaluation=EVALUATION, trace=TRACE)
    assert replay.experience_id == candidate.experience_id, "extraction not idempotent"
    assert candidate.to_document()["status"] == "CANDIDATE"

    database = SQLiteDatabase(Path(tempfile.mkdtemp(prefix="x02-smoke-")) / "smoke.db")
    await database.initialize()
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, 'correlation-1')", (stamp, stamp))
        await tx.execute("INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, 'correlation-1')", (stamp,))
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', 'task-1', '{}', 'ACTIVE', ?, ?, 'correlation-1')",
            (stamp, stamp),
        )
        await tx.execute(
            """INSERT INTO task(task_id,grant_id,status,version,attempt,created_at,
                                 finished_at,deadline,correlation_id)
               VALUES ('task-1','grant','SUCCEEDED',1,1,?,?,?,?)""",
            (stamp, stamp, stamp, "correlation-1"),
        )
        await tx.execute(
            "INSERT INTO episode VALUES ('episode-1', 'task-1', '{}', ?, 'correlation-1')",
            (stamp,),
        )
    repair = RestRepair(database, FakeClock(NOW), FakeUuidGenerator(
        UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(300, 380)
    ))
    decision = await repair.prepare(DAY, mode="REVIEW", phase="CLOSED")
    assert decision.outcome is RepairOutcome.REQUESTED and decision.request is not None
    await repair.complete(
        decision.request.run_id, result={},
        candidate_experiences=(candidate.to_document(),),
    )
    await database.close()
    print("X-02 packaging smoke PASS:", json.dumps({
        "experience_id": candidate.experience_id,
        "quality_score": candidate.quality_score,
        "rest_repair": "SUCCEEDED",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
