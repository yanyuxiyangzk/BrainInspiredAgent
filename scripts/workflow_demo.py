"""JSON-driven workflow live demo: load a workflow from a .json file and run it.

Writes a workflow document (all five node types: condition / delay / parallel /
skill / sub_workflow) to JSON files, loads them back, registers them in the
WorkflowRegistry and executes with the real WorkflowRuntime + sample skills.
Prints the per-node execution evidence and the final output mapping.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sample_domain import SUMMARY_CAPABILITY, install_sample_skills

from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.foundation.identity import FakeUuidGenerator
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runtime import (
    WorkflowExecutionRequest,
    WorkflowRuntime,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        from datetime import timedelta

        self.current += timedelta(seconds=seconds)


class Logger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields

    def warning(self, message: str, **fields: object) -> None:
        del message, fields

    def error(self, message: str, **fields: object) -> None:
        del message, fields


CHILD_JSON = {
    "spec_version": "1.0",
    "workflow_id": "child_flow",
    "version": "1.0.0",
    "name": "Child flow",
    "input_schema": {"type": "object"},
    "policy": {
        "timeout_seconds": 30,
        "max_parallelism": 2,
        "required_capabilities": [SUMMARY_CAPABILITY],
    },
    "nodes": [
        {
            "node_id": "child_skill",
            "type": "skill",
            "depends_on": [],
            "capability": SUMMARY_CAPABILITY,
            "capability_version": "1.0",
            "input": {"title": "child", "items": "$.params.items"},
            "constraints": {"side_effect": "PURE"},
        }
    ],
    "output_mapping": {"text": "$.nodes.child_skill.output.summary"},
}

ROOT_JSON = {
    "spec_version": "1.0",
    "workflow_id": "root_flow",
    "version": "1.0.0",
    "name": "Root flow (all five node types)",
    "input_schema": {"type": "object"},
    "policy": {
        "timeout_seconds": 60,
        "max_parallelism": 2,
        "required_capabilities": [SUMMARY_CAPABILITY],
    },
    "nodes": [
        {
            "node_id": "choose",
            "type": "condition",
            "depends_on": [],
            "expression": "$.params.enabled == true",
            "then": ["wait"],
            "else": ["unused"],
        },
        {
            "node_id": "wait",
            "type": "delay",
            "depends_on": ["choose"],
            "duration_seconds": 0.1,
        },
        {
            "node_id": "unused",
            "type": "skill",
            "depends_on": ["choose"],
            "capability": SUMMARY_CAPABILITY,
            "capability_version": "1.0",
            "input": {"title": "unused-branch", "items": ["never"]},
            "constraints": {"side_effect": "PURE"},
        },
        {
            "node_id": "fanout",
            "type": "parallel",
            "depends_on": ["wait"],
            "branches": [["left"], ["right"]],
            "failure_policy": "min_success",
            "min_success": 1,
        },
        {
            "node_id": "left",
            "type": "skill",
            "depends_on": ["fanout"],
            "capability": SUMMARY_CAPABILITY,
            "capability_version": "1.0",
            "input": {"title": "left", "items": ["alpha", "beta"]},
            "constraints": {"side_effect": "PURE"},
        },
        {
            "node_id": "right",
            "type": "skill",
            "depends_on": ["fanout"],
            "capability": SUMMARY_CAPABILITY,
            "capability_version": "1.0",
            "input": {"title": "right", "items": []},
            "constraints": {"side_effect": "PURE"},
        },
        {
            "node_id": "child",
            "type": "sub_workflow",
            "depends_on": ["fanout"],
            "workflow_id": "child_flow",
            "workflow_version": "1.0.0",
            "input": {"items": ["nested"]},
            "failure_policy": "propagate",
        },
    ],
    "output_mapping": {
        "left_summary": "$.nodes.left.output.summary",
        "right_count": "$.nodes.right.output.item_count",
        "child_text": "$.nodes.child.output.output.text",
    },
}


async def seed_task(database: SQLiteDatabase) -> None:
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO plan VALUES ('plan', '{}', 'd', 'APPROVED', 'now', 'later', 'corr')"
        )
        await tx.execute(
            "INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', 'now', 'corr')"
        )
        await tx.execute(
            "INSERT INTO execution_grant VALUES "
            "('grant', 'decision', 'task', '{}', 'ACTIVE', 'now', 'later', 'corr')"
        )
        await tx.execute(
            "INSERT INTO task(task_id, grant_id, status, created_at, deadline, correlation_id) "
            "VALUES ('task', 'grant', 'PENDING', 'now', 'later', 'corr')"
        )


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="workflow-demo-"))
    # 1) 工作流定义落盘为 JSON 文件——JSON 驱动的字面证明
    (tmp / "child_flow.json").write_text(json.dumps(CHILD_JSON, indent=1), encoding="utf-8")
    (tmp / "root_flow.json").write_text(json.dumps(ROOT_JSON, indent=1), encoding="utf-8")
    child_document = json.loads((tmp / "child_flow.json").read_text(encoding="utf-8"))
    root_document = json.loads((tmp / "root_flow.json").read_text(encoding="utf-8"))

    # 2) 注册（静态校验 + DAG 检查）
    registry = WorkflowRegistry()
    registry.register(child_document, status=WorkflowStatus.VALIDATED)
    root = registry.register(root_document, status=WorkflowStatus.VALIDATED)

    # 3) 装配样例技能与运行时
    database = SQLiteDatabase(tmp / "facts.db")
    await database.initialize()
    await seed_task(database)
    clock = Clock()
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_sample_skills(capabilities, skills, clock=clock)  # type: ignore[arg-type]
    artifacts = LocalArtifactStore(
        tmp / "objects", inline_limit_bytes=80, max_artifact_bytes=2_000_000
    )
    context = SkillContext(
        clock, Logger(), CancellationToken(), artifacts, {}, ResourceBudget(10)  # type: ignore[arg-type]
    )
    runtime = WorkflowRuntime(
        database=database,
        registry=registry,
        skill_invoker=SkillInvoker(skills, bundle.adapters),
        skill_context=context,
        artifacts=artifacts,
        clock=clock,  # type: ignore[arg-type]
        identifiers=FakeUuidGenerator(UUID(int=i) for i in range(1, 300)),
    )

    # 4) 解析技能绑定并创建运行行
    resolver = SkillResolver(capabilities, skills, clock=clock)  # type: ignore[arg-type]
    bindings = {}
    for definition in registry.all():
        for node in definition.definition["nodes"]:
            if node["type"] != "skill":
                continue
            bindings[(definition.workflow_id, definition.version, node["node_id"])] = (
                resolver.resolve(
                    SkillRequirement(
                        node["node_id"], SUMMARY_CAPABILITY, "1.0", frozenset(), SideEffect.PURE
                    ),
                    policy_version="demo",
                )
            )
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO workflow_run(run_id,task_id,workflow_id,workflow_version,"
            "workflow_digest,input_digest,status,deadline,created_at,correlation_id) "
            "VALUES ('root-run','task',?,?,?,?, 'PENDING',?,?,?)",
            (
                root.workflow_id, root.version, root.digest, "digest",
                (NOW + timedelta(minutes=5)).isoformat(), NOW.isoformat(), "corr",
            ),
        )

    # 5) 执行
    result = await runtime.execute(
        WorkflowExecutionRequest(
            "root-run", "task", root, {"enabled": True}, bindings,
            NOW + timedelta(minutes=5), "corr",
        )
    )
    rows = await database.fetch_all(
        "SELECT node_id, status FROM node_run WHERE run_id='root-run' ORDER BY rowid"
    )
    print(json.dumps({
        "loaded_from": [str(tmp / "child_flow.json"), str(tmp / "root_flow.json")],
        "status": result.status.value,
        "nodes": {str(row["node_id"]): str(row["status"]) for row in rows},
        "output": result.output,
    }, indent=1, ensure_ascii=False))
    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
