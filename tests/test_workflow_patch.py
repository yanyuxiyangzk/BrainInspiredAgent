"""X-01 tests: versioned WorkflowPatch — the minimal-diff contract over a workflow JSON.

三层验收：
1. Schema 契约——正反例文档直接对 ``schemas/evolution/workflow-patch-1.0.schema.json``
   （阶段 0 冻结契约）做 Draft 2020-12 校验，格式检查含 uuid/uniqueItems/minItems；
2. 代码模型——``domain_sdk.workflow_patch.WorkflowPatch.parse`` 与 Schema 同约束，
   并补充跨字段语义（add/remove/replace 的 path/value 存在性、摘要口径）；
3. 应用语义——``apply_patch`` 只允许白名单操作，钉死 base digest，拒绝
   side_effect/required_permissions 等安全字段，结果必须通过 WorkflowValidator。
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from domain_sdk import PatchBase, PatchOperation, WorkflowPatch, WorkflowPatchError
from domain_sdk.workflow_patch import apply_patch, digest_document

ROOT = Path(__file__).parents[1]
SCHEMA = json.loads(
    (ROOT / "schemas" / "evolution" / "workflow-patch-1.0.schema.json").read_text(encoding="utf-8")
)

WORKFLOW: dict[str, object] = {
    "spec_version": "1.0",
    "workflow_id": "patch_flow",
    "version": "1.0.0",
    "name": "Patch flow",
    "input_schema": {"type": "object"},
    "policy": {"timeout_seconds": 10, "max_parallelism": 1,
               "required_capabilities": ["demo.summarize"]},
    "nodes": [
        {"node_id": "fetch", "type": "skill", "depends_on": [],
         "capability": "demo.summarize", "capability_version": "1.0",
         "input": {"source": "$.params.source"},
         "constraints": {"side_effect": "PURE"}},
        {"node_id": "summary", "type": "skill", "depends_on": ["fetch"],
         "capability": "demo.summarize", "capability_version": "1.0",
         "input": {"title": "raw", "items": "$.nodes.fetch.output.records",
                   "meta": {"lang": "en"}},
         "constraints": {"side_effect": "PURE"}},
    ],
    "output_mapping": {"text": "$.nodes.summary.output.summary"},
}


def patch_document(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "proposal_id": "00000000-0000-0000-0000-000000000001",
        "base": {"workflow_id": "patch_flow", "version": "1.0.0",
                 "digest": digest_document(WORKFLOW)},
        "source": "OUTCOME_EVALUATION",
        "hypothesis": "Tighten the summary input to raise user value.",
        "operations": [
            {"op": "replace_input", "node_id": "summary", "path": "input.title",
             "value": "patched"},
            {"op": "replace_constraint", "node_id": "summary",
             "path": "constraints.max_latency_ms", "value": 2500},
        ],
        "required_evidence": ["replay:30-trading-days"],
        "requested_capabilities": [],
    }
    document.update(changes)
    return document


def _schema_errors(document: Mapping[str, object]) -> list[str]:
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(document)]


# ---------------------------------------------------------------------------
# 1) Schema 契约：正反例
# ---------------------------------------------------------------------------


def test_schema_accepts_the_architecture_example() -> None:
    assert _schema_errors(patch_document()) == []


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"schema_version": "2.0"}, "was expected"),
        ({"proposal_id": "not-a-uuid"}, "is not a"),
        ({"source": "MAGIC"}, "is not one of"),
        ({"hypothesis": ""}, "should be non-empty"),
        ({"hypothesis": "x" * 1001}, "too long"),
        ({"required_evidence": "replay"}, "is not of type"),
        ({"requested_capabilities": ["demo.summarize", "demo.summarize"]},
         "non-unique"),
        ({"operations": []}, "should be non-empty"),
        ({"extra_key": 1}, "Additional properties"),
        ({"base": {"workflow_id": "patch_flow"}}, "is a required property"),
    ],
)
def test_schema_rejects_broken_documents(
    mutation: dict[str, object], fragment: str,
) -> None:
    document = patch_document()
    document.update(mutation)
    assert any(fragment in message for message in _schema_errors(document)), (
        _schema_errors(document)
    )


def test_schema_rejects_unknown_operation_and_extra_operation_fields() -> None:
    document = patch_document()
    operations = document["operations"]
    assert isinstance(operations, list)
    operations[0]["op"] = "replace_policy"
    assert any("is not one of" in message for message in _schema_errors(document))
    document = patch_document()
    operations = document["operations"]
    assert isinstance(operations, list)
    operations[0]["grant"] = "escalate"
    assert any("Additional properties" in message for message in _schema_errors(document))


# ---------------------------------------------------------------------------
# 2) 代码模型：同约束解析 + 语义层
# ---------------------------------------------------------------------------


def test_parse_returns_immutable_patch_with_stable_digest() -> None:
    document = patch_document()
    patch = WorkflowPatch.parse(document)
    assert patch.proposal_id == "00000000-0000-0000-0000-000000000001"
    assert patch.base == PatchBase("patch_flow", "1.0.0", digest_document(WORKFLOW))
    assert patch.source == "OUTCOME_EVALUATION"
    assert patch.operations == (
        PatchOperation("replace_input", "summary", "input.title", "patched"),
        PatchOperation("replace_constraint", "summary", "constraints.max_latency_ms", 2500),
    )
    assert patch.patch_digest == digest_document(document)
    assert patch.to_document() == document
    assert WorkflowPatch.parse(patch.to_document()) == patch


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"schema_version": "2.0"}, "schema_version"),
        ({"proposal_id": "nope"}, "proposal_id"),
        ({"source": "MAGIC"}, "source"),
        ({"hypothesis": ""}, "hypothesis"),
        ({"required_evidence": [1]}, "required_evidence"),
        ({"requested_capabilities": ["a", "a"]}, "requested_capabilities"),
        ({"operations": []}, "operations"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_parse_mirrors_schema_constraints(
    mutation: dict[str, object], fragment: str,
) -> None:
    with pytest.raises(WorkflowPatchError, match=fragment):
        WorkflowPatch.parse(patch_document(**mutation))


def test_parse_enforces_operation_field_semantics() -> None:
    base = patch_document()
    operations = base["operations"]
    assert isinstance(operations, list)
    del operations[1]["value"]
    with pytest.raises(WorkflowPatchError, match="value"):
        WorkflowPatch.parse(base)

    removal = patch_document()
    removal_operations = removal["operations"]
    assert isinstance(removal_operations, list)
    removal_operations[0] = {
        "op": "remove_node", "node_id": "summary", "value": {"unexpected": True},
    }
    with pytest.raises(WorkflowPatchError, match="remove_node"):
        WorkflowPatch.parse(removal)

    addition = patch_document()
    addition_operations = addition["operations"]
    assert isinstance(addition_operations, list)
    addition_operations[0] = {"op": "add_node", "node_id": "enrich"}
    with pytest.raises(WorkflowPatchError, match="node document"):
        WorkflowPatch.parse(addition)


def test_parse_rejects_non_object_documents_and_missing_fields() -> None:
    with pytest.raises(WorkflowPatchError, match="must be an object"):
        WorkflowPatch.parse("nope")  # type: ignore[arg-type]
    document = patch_document()
    del document["source"]
    with pytest.raises(WorkflowPatchError, match="missing patch fields"):
        WorkflowPatch.parse(document)


def test_parse_rejects_invalid_base_documents() -> None:
    with pytest.raises(WorkflowPatchError, match="base must define"):
        WorkflowPatch.parse(patch_document(base=["nope"]))
    with pytest.raises(WorkflowPatchError, match="identity"):
        WorkflowPatch.parse(patch_document(base={
            "workflow_id": "", "version": "1.0.0", "digest": "sha256:" + "0" * 64,
        }))
    with pytest.raises(WorkflowPatchError, match="digest must be a sha256"):
        WorkflowPatch.parse(patch_document(base={
            "workflow_id": "patch_flow", "version": "1.0.0", "digest": "nope",
        }))


@pytest.mark.parametrize(
    ("operations", "fragment"),
    [
        ([1], "must be an object"),
        ([{"op": "remove_node", "node_id": "x", "grant": 1}], "unexpected operation fields"),
        ([{"node_id": "x"}], "requires op and node_id"),
        ([{"op": "replace_policy", "node_id": "x"}], "unknown patch operation"),
        ([{"op": "remove_node", "node_id": ""}], "node_id must not be empty"),
        ([{"op": "replace_input", "node_id": "x", "value": 1}], "requires a path"),
    ],
)
def test_parse_rejects_malformed_operations(
    operations: list[object], fragment: str,
) -> None:
    with pytest.raises(WorkflowPatchError, match=fragment):
        WorkflowPatch.parse(patch_document(operations=operations))


def test_round_trip_omits_absent_path_and_value() -> None:
    document = patch_document()
    document["operations"] = [{"op": "remove_node", "node_id": "summary"}]
    patch = WorkflowPatch.parse(document)
    assert patch.to_document() == document
    assert patch.operations == (PatchOperation("remove_node", "summary"),)


# ---------------------------------------------------------------------------
# 3) 应用语义：白名单操作 + base 钉死 + WorkflowValidator
# ---------------------------------------------------------------------------


def test_apply_patch_is_deterministic_and_leaves_base_untouched() -> None:
    patch = WorkflowPatch.parse(patch_document())
    frozen = copy.deepcopy(WORKFLOW)
    first = apply_patch(patch, WORKFLOW, new_version="1.0.1")
    second = apply_patch(patch, WORKFLOW, new_version="1.0.1")
    assert WORKFLOW == frozen
    assert first == second
    assert first["version"] == "1.0.1"
    nodes = {str(node["node_id"]): node for node in first["nodes"]}  # type: ignore[index]
    assert nodes["summary"]["input"]["title"] == "patched"  # type: ignore[index]
    assert nodes["summary"]["constraints"]["max_latency_ms"] == 2500  # type: ignore[index]


def test_apply_patch_adds_and_rewires_nodes() -> None:
    document = patch_document()
    operations = document["operations"]
    assert isinstance(operations, list)
    operations.clear()
    operations.append({
        "op": "add_node", "node_id": "enrich",
        "value": {"node_id": "enrich", "type": "skill", "depends_on": ["fetch"],
                  "capability": "demo.summarize", "capability_version": "1.0",
                  "input": {"title": "enriched"},
                  "constraints": {"side_effect": "PURE"}},
    })
    operations.append({
        "op": "replace_dependency", "node_id": "summary", "path": "depends_on",
        "value": ["fetch", "enrich"],
    })
    operations.append({
        "op": "replace_input", "node_id": "summary", "path": "input.meta.lang",
        "value": "zh",
    })
    patched = apply_patch(
        WorkflowPatch.parse(document), WORKFLOW, new_version="1.1.0"
    )
    nodes = {str(node["node_id"]): node for node in patched["nodes"]}  # type: ignore[index]
    assert nodes["summary"]["depends_on"] == ["fetch", "enrich"]  # type: ignore[index]
    assert nodes["summary"]["input"]["meta"] == {"lang": "zh"}  # type: ignore[index]


def test_apply_patch_supports_operation_sequences() -> None:
    document = patch_document()
    operations = document["operations"]
    assert isinstance(operations, list)
    operations.clear()
    operations.append({
        "op": "add_node", "node_id": "enrich",
        "value": {"node_id": "enrich", "type": "skill", "depends_on": ["fetch"],
                  "capability": "demo.summarize", "capability_version": "1.0",
                  "input": {}, "constraints": {"side_effect": "PURE"}},
    })
    operations.append({"op": "remove_node", "node_id": "enrich"})
    operations.append({
        "op": "replace_constraint", "node_id": "fetch",
        "path": "constraints.freshness_seconds", "value": 30,
    })
    patched = apply_patch(WorkflowPatch.parse(document), WORKFLOW, new_version="1.0.1")
    nodes = {str(node["node_id"]): node for node in patched["nodes"]}  # type: ignore[index]
    assert set(nodes) == {"fetch", "summary"}
    assert nodes["fetch"]["constraints"]["freshness_seconds"] == 30  # type: ignore[index]


def test_apply_patch_rejects_digest_or_identity_mismatch() -> None:
    tampered_base = patch_document(base={
        "workflow_id": "patch_flow", "version": "1.0.0",
        "digest": "sha256:" + "0" * 64,
    })
    with pytest.raises(WorkflowPatchError, match="digest"):
        apply_patch(WorkflowPatch.parse(tampered_base), WORKFLOW, new_version="1.0.1")

    renamed = patch_document(base={
        "workflow_id": "other_flow", "version": "1.0.0",
        "digest": digest_document(WORKFLOW),
    })
    with pytest.raises(WorkflowPatchError, match="workflow_id"):
        apply_patch(WorkflowPatch.parse(renamed), WORKFLOW, new_version="1.0.1")

    wrong_version = patch_document(base={
        "workflow_id": "patch_flow", "version": "9.9.9",
        "digest": digest_document(WORKFLOW),
    })
    with pytest.raises(WorkflowPatchError, match="version"):
        apply_patch(WorkflowPatch.parse(wrong_version), WORKFLOW, new_version="1.0.1")


def test_apply_patch_rejects_unknown_nodes_and_security_paths() -> None:
    unknown = patch_document()
    operations = unknown["operations"]
    assert isinstance(operations, list)
    operations[0]["node_id"] = "absent"
    with pytest.raises(WorkflowPatchError, match="absent"):
        apply_patch(WorkflowPatch.parse(unknown), WORKFLOW, new_version="1.0.1")

    escalation = patch_document()
    escalation_operations = escalation["operations"]
    assert isinstance(escalation_operations, list)
    escalation_operations[1] = {
        "op": "replace_constraint", "node_id": "summary",
        "path": "constraints.side_effect", "value": "NON_REPLAYABLE",
    }
    with pytest.raises(WorkflowPatchError, match="not mutable"):
        apply_patch(WorkflowPatch.parse(escalation), WORKFLOW, new_version="1.0.1")

    permissions = patch_document()
    permission_operations = permissions["operations"]
    assert isinstance(permission_operations, list)
    permission_operations[1] = {
        "op": "replace_constraint", "node_id": "summary",
        "path": "constraints.required_permissions", "value": ["write:all"],
    }
    with pytest.raises(WorkflowPatchError, match="not mutable"):
        apply_patch(WorkflowPatch.parse(permissions), WORKFLOW, new_version="1.0.1")

    stray_path = patch_document()
    stray_operations = stray_path["operations"]
    assert isinstance(stray_operations, list)
    stray_operations[1] = {
        "op": "replace_constraint", "node_id": "summary",
        "path": "policy.timeout_seconds", "value": 1,
    }
    with pytest.raises(WorkflowPatchError, match="path"):
        apply_patch(WorkflowPatch.parse(stray_path), WORKFLOW, new_version="1.0.1")


def test_apply_patch_rejects_results_breaking_the_workflow_contract() -> None:
    removal = patch_document()
    operations = removal["operations"]
    assert isinstance(operations, list)
    operations.clear()
    operations.append({"op": "remove_node", "node_id": "summary"})
    with pytest.raises(WorkflowPatchError, match="invalid"):
        apply_patch(WorkflowPatch.parse(removal), WORKFLOW, new_version="1.0.1")

    duplicate = patch_document()
    duplicate_operations = duplicate["operations"]
    assert isinstance(duplicate_operations, list)
    duplicate_operations.clear()
    duplicate_operations.append({
        "op": "add_node", "node_id": "fetch",
        "value": {"node_id": "fetch", "type": "skill", "depends_on": [],
                  "capability": "demo.summarize", "capability_version": "1.0",
                  "input": {}, "constraints": {"side_effect": "PURE"}},
    })
    with pytest.raises(WorkflowPatchError, match="duplicate"):
        apply_patch(WorkflowPatch.parse(duplicate), WORKFLOW, new_version="1.0.1")


def test_apply_patch_rejects_bad_value_shapes() -> None:
    mismatched = patch_document()
    mismatched["operations"] = [{
        "op": "add_node", "node_id": "enrich", "value": {"node_id": "elsewhere"},
    }]
    with pytest.raises(WorkflowPatchError, match="carry the operation node_id"):
        apply_patch(WorkflowPatch.parse(mismatched), WORKFLOW, new_version="1.0.1")

    deep = patch_document()
    deep["operations"] = [{
        "op": "replace_input", "node_id": "summary", "path": "input.records.deep",
        "value": 1,
    }]
    with pytest.raises(WorkflowPatchError, match="non-object"):
        apply_patch(WorkflowPatch.parse(deep), WORKFLOW, new_version="1.0.1")

    wrong_dependency_path = patch_document()
    wrong_dependency_path["operations"] = [{
        "op": "replace_dependency", "node_id": "summary", "path": "depends", "value": [],
    }]
    with pytest.raises(WorkflowPatchError, match="depends_on"):
        apply_patch(WorkflowPatch.parse(wrong_dependency_path), WORKFLOW, new_version="1.0.1")

    scalar_dependency = patch_document()
    scalar_dependency["operations"] = [{
        "op": "replace_dependency", "node_id": "summary", "path": "depends_on",
        "value": "fetch",
    }]
    with pytest.raises(WorkflowPatchError, match="list of node IDs"):
        apply_patch(WorkflowPatch.parse(scalar_dependency), WORKFLOW, new_version="1.0.1")
