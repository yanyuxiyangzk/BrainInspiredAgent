"""X-01 packaging smoke: exercise WorkflowPatch against the wheel-installed SDK.

在全新 venv（仅安装 bia wheel）中验证 WorkflowPatch 契约：解析冻结 Schema
口径的补丁文档 → 校验 base digest 钉死 → 应用白名单操作 → 结果通过
WorkflowValidator。不导入 tests/。

用法：python scripts/x01_packaging_smoke.py
"""
from __future__ import annotations

import asyncio
import json

from domain_sdk import (
    PatchBase,
    PatchOperation,
    WorkflowPatch,
    WorkflowPatchError,
    apply_patch,
    digest_document,
)

WORKFLOW: dict[str, object] = {
    "spec_version": "1.0",
    "workflow_id": "x01_smoke",
    "version": "1.0.0",
    "name": "X-01 smoke",
    "input_schema": {"type": "object"},
    "policy": {"timeout_seconds": 10, "max_parallelism": 1,
               "required_capabilities": ["demo.summarize"]},
    "nodes": [
        {"node_id": "summary", "type": "skill", "depends_on": [],
         "capability": "demo.summarize", "capability_version": "1.0",
         "input": {"title": "raw"}, "constraints": {"side_effect": "PURE"}},
    ],
    "output_mapping": {"text": "$.nodes.summary.output.summary"},
}


async def main() -> int:
    patch = WorkflowPatch.parse({
        "schema_version": "1.0",
        "proposal_id": "00000000-0000-0000-0000-000000000001",
        "base": {"workflow_id": "x01_smoke", "version": "1.0.0",
                 "digest": digest_document(WORKFLOW)},
        "source": "GOAL_DESIGN",
        "hypothesis": "Sharpen the summary title.",
        "operations": [{"op": "replace_input", "node_id": "summary",
                        "path": "input.title", "value": "patched"}],
        "required_evidence": ["replay:smoke"],
        "requested_capabilities": [],
    })
    assert patch.operations == (
        PatchOperation("replace_input", "summary", "input.title", "patched"),
    )
    assert patch.base == PatchBase("x01_smoke", "1.0.0", digest_document(WORKFLOW))
    patched = apply_patch(patch, WORKFLOW, new_version="1.0.1")
    assert patched["version"] == "1.0.1"
    nodes = patched["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    assert nodes[0]["input"] == {"title": "patched"}

    tampered = WorkflowPatch.parse({
        "schema_version": "1.0",
        "proposal_id": "00000000-0000-0000-0000-000000000002",
        "base": {"workflow_id": "x01_smoke", "version": "1.0.0",
                 "digest": "sha256:" + "0" * 64},
        "source": "HUMAN", "hypothesis": "Tampered base.",
        "operations": [{"op": "remove_node", "node_id": "summary"}],
        "required_evidence": [], "requested_capabilities": [],
    })
    try:
        apply_patch(tampered, WORKFLOW, new_version="1.0.1")
    except WorkflowPatchError as error:
        assert "digest" in str(error), error
    else:  # pragma: no cover - guard
        raise AssertionError("tampered base digest was accepted")
    print("X-01 packaging smoke PASS:", json.dumps({
        "patch_digest": patch.patch_digest, "patched_version": patched["version"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
