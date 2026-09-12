"""G-01 packaging smoke: drive the wheel-installed `brainagent evolution auto-plan`.

直接对已安装 wheel 的 CLI 做黑盒验证（不导入 tests/）：内联一份中性工作流，
注册 ACTIVE 基线、播种完整 FK 链的 fitness 事实与经验数据集，然后执行
`brainagent evolution auto-plan` 并断言返回 PROPOSED。

用法（在全新 venv 中，使用已安装的 bia wheel）：
    python scripts/g01_packaging_smoke.py <venv-bin>/brainagent
不传参数时只播种并打印提示（用于手动演练）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import DnaDefinition, DnaParent, DnaStatus, PersistentDnaRegistry
from domain_sdk.experience_dataset import ExperienceDatasetBuilder, ExperienceDatasetSpec

NOW = datetime(2026, 9, 12, 10, tzinfo=UTC)
DNA_ID = "workflow.g01_smoke"
WINDOW = "g01-smoke-window"


def _workflow(version: str) -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "g01_smoke", "version": version,
        "name": "G-01 smoke", "input_schema": {"type": "object"},
        "policy": {"timeout_seconds": 10, "max_parallelism": 1,
                   "required_capabilities": ["demo.summarize"]},
        "nodes": [{"node_id": "summary", "type": "skill", "depends_on": [],
                   "capability": "demo.summarize", "capability_version": "1.0",
                   "input": {}, "constraints": {"side_effect": "PURE"}}],
        "output_mapping": {"summary": "$.nodes.summary.output"},
    }


def _variant(version: str, digest_parent: DnaDefinition) -> dict[str, object]:
    document = _workflow(version)
    nodes = document["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    nodes[0]["input"] = {"title": "smoke variant"}
    document["parent_dna"] = [{
        "dna_id": digest_parent.dna_id, "version": digest_parent.version,
        "content_digest": digest_parent.content_digest,
    }]
    return document


async def _seed(database_path: Path) -> Path:
    database = SQLiteDatabase(database_path)
    await database.initialize()
    clock = FakeClock(NOW)
    registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=i) for i in range(1, 50))
    )
    baseline_record = await registry.register(
        DnaDefinition.from_workflow(_workflow("1.0.0"), dna_id=DNA_ID, version="1.0.0"),
        correlation_id="smoke:baseline",
    )
    for status in (DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY):
        baseline_record = await registry.transition(
            DNA_ID, baseline_record.dna.version, status,
            expected_revision=baseline_record.revision,
            reason="smoke baseline", correlation_id="smoke:baseline",
        )
    baseline_record = await registry.activate(
        DNA_ID, baseline_record.dna.version,
        expected_revision=baseline_record.revision,
        reason="smoke baseline", correlation_id="smoke:baseline",
    )
    baseline = baseline_record.dna
    variant = await registry.register(
        DnaDefinition.from_workflow(
            _variant("1.0.1", baseline), dna_id=DNA_ID, version="1.0.1",
            parent_dna=(DnaParent(DNA_ID, baseline.version, baseline.content_digest),),
        ),
        correlation_id="smoke:variant",
    )

    stamp = NOW.isoformat().replace("+00:00", "Z")
    for definition, tag in ((baseline, "base"), (variant.dna, "cand")):
        correlation = f"smoke-correlation-{tag}"
        evaluation = {
            "evaluation_id": f"evaluation-{tag}", "episode_id": f"episode-{tag}",
            "task_id": f"task-{tag}", "correlation_id": correlation,
            "evaluated_at": stamp, "evaluator_version": "rules/1",
            "task_status": "SUCCEEDED", "goal_id": f"goal-{tag}",
            "evidence_ids": [f"evidence-{tag}"],
            "execution": {"status": "PASSED", "score": 1.0, "reasons": ["smoke"]},
            "goal": {"status": "PASSED", "score": 1.0, "reasons": ["smoke"]},
            "quality": {"status": "PASSED", "score": 0.8, "reasons": ["smoke"]},
            "evidence": {"status": "PASSED", "score": 1.0, "reasons": ["smoke"]},
        }
        async with database.transaction() as tx:
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
            await tx.execute(
                "INSERT INTO outcome_evaluation VALUES (?,?,?,?,?,?)",
                (f"evaluation-{tag}", f"task-{tag}", f"episode-{tag}",
                 json.dumps(evaluation), stamp, correlation),
            )
            await tx.execute(
                "INSERT INTO dna_fitness_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"observation-{tag}", f"evaluation-{tag}", f"task-{tag}",
                 definition.dna_id, definition.version, definition.content_digest, WINDOW,
                 1, 1.0, 0.8, 10, 100, 1, "[]", stamp,
                 f"sha256:observation-{tag}", correlation),
            )
    async with database.transaction() as tx:
        await tx.execute(
            """INSERT INTO dna_fitness_snapshot VALUES (
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
               )""",
            (DNA_ID, baseline.version, baseline.content_digest, WINDOW,
             "fitness/1", 2, 1.0, 0.95, 1.0, 0.4, 10.0, 100.0, 100,
             1.0, 0.0, "READY", stamp, 1),
        )
    await ExperienceDatasetBuilder(database, clock).build(
        ExperienceDatasetSpec(
            dataset_id="g01-smoke-ds", version="1.0.0",
            builder_version="g01-packaging-smoke/1.0", window_id=WINDOW,
            starts_at=NOW - timedelta(minutes=5),
            train_until=NOW + timedelta(minutes=5),
            validation_until=NOW + timedelta(minutes=10),
            ends_at=NOW + timedelta(minutes=15),
            baseline_content_digest=baseline.content_digest,
            candidate_content_digests=(variant.dna.content_digest,),
        )
    )
    await database.close()
    return database_path


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="g01-smoke-"))
    database_path = await _seed(workdir / "g01.db")
    if len(sys.argv) < 2:
        print(json.dumps({"database": str(database_path), "status": "SEEDED"}))
        return 0
    process = await asyncio.create_subprocess_exec(
        sys.argv[1], "--database", str(database_path),
        "evolution", "auto-plan", "g01-smoke-proposal-1",
        "--baseline", DNA_ID, "--dataset-id", "g01-smoke-ds",
        "--dataset-version", "1.0.0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    payload = json.loads(stdout)
    assert payload["status"] == "PROPOSED", payload
    assert payload["weakness"] == "user_value_score", payload
    print("G-01 packaging CLI smoke PASS:", json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
