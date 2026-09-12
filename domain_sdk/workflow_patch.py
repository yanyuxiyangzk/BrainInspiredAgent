"""X-01: versioned WorkflowPatch — the minimal-diff contract over a workflow JSON.

冻结契约见 ``schemas/evolution/workflow-patch-1.0.schema.json``（阶段 0）与
[可进化工作流架构 §11](docs/architecture/evolvable-workflow-skill-architecture.md)：
白名单五操作（add_node/remove_node/replace_constraint/replace_input/
replace_dependency），不提供任意 JSON Patch；patch 永远钉死 base
（workflow_id/version/canonical digest）；``side_effect``、``required_permissions``
等安全字段不可变；补丁结果必须再次通过 WorkflowValidator。进化链路上只保存
结构化 patch，配合不可变版本即可回溯"为什么从旧版本变成现在这样"。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from active_agent_platform.workflow import WorkflowValidationError, WorkflowValidator

_OPS = frozenset({
    "add_node", "remove_node", "replace_constraint", "replace_input", "replace_dependency",
})
_SOURCES = frozenset({"GOAL_DESIGN", "OUTCOME_EVALUATION", "FAILURE_ANALYSIS", "HUMAN"})
_FIELDS = frozenset({"schema_version", "proposal_id", "base", "source", "hypothesis",
                     "operations", "required_evidence", "requested_capabilities"})
_OPERATION_FIELDS = frozenset({"op", "node_id", "path", "value"})
_BASE_FIELDS = frozenset({"workflow_id", "version", "digest"})
_IMMUTABLE_CONSTRAINTS = frozenset({"side_effect", "required_permissions"})
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class WorkflowPatchError(ValueError):
    pass


def digest_document(document: Mapping[str, object]) -> str:
    """Canonical content digest shared by patches, bases and patched results."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PatchBase:
    workflow_id: str
    version: str
    digest: str


@dataclass(frozen=True, slots=True)
class PatchOperation:
    op: str
    node_id: str
    path: str | None = None
    value: object = None


@dataclass(frozen=True, slots=True)
class WorkflowPatch:
    proposal_id: str
    base: PatchBase
    source: str
    hypothesis: str
    operations: tuple[PatchOperation, ...]
    required_evidence: tuple[str, ...]
    requested_capabilities: tuple[str, ...]
    patch_digest: str

    @classmethod
    def parse(cls, document: Mapping[str, object]) -> WorkflowPatch:
        if not isinstance(document, Mapping):
            raise WorkflowPatchError("workflow patch must be an object")
        unknown = sorted(set(map(str, document)) - _FIELDS)
        if unknown:
            raise WorkflowPatchError(f"unexpected patch fields: {', '.join(unknown)}")
        missing = sorted(_FIELDS - set(map(str, document)))
        if missing:
            raise WorkflowPatchError(f"missing patch fields: {', '.join(missing)}")
        if document["schema_version"] != "1.0":
            raise WorkflowPatchError("unsupported patch schema_version")
        proposal_id = document["proposal_id"]
        if not isinstance(proposal_id, str) or _UUID.fullmatch(proposal_id) is None:
            raise WorkflowPatchError("patch proposal_id must be a UUID")
        base = _base(document["base"])
        source = document["source"]
        if source not in _SOURCES:
            raise WorkflowPatchError(f"unknown patch source: {source}")
        hypothesis = document["hypothesis"]
        if not isinstance(hypothesis, str) or not 1 <= len(hypothesis) <= 1000:
            raise WorkflowPatchError("patch hypothesis must be 1..1000 characters")
        operations = _operations(document["operations"])
        evidence = _strings(document["required_evidence"], "required_evidence")
        capabilities = _strings(document["requested_capabilities"], "requested_capabilities")
        if len(set(capabilities)) != len(capabilities):
            raise WorkflowPatchError("requested_capabilities must be unique")
        return cls(proposal_id, base, str(source), hypothesis, operations,
                   evidence, capabilities, digest_document(document))

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "proposal_id": self.proposal_id,
            "base": {"workflow_id": self.base.workflow_id,
                     "version": self.base.version, "digest": self.base.digest},
            "source": self.source,
            "hypothesis": self.hypothesis,
            "operations": [operation_document(operation) for operation in self.operations],
            "required_evidence": list(self.required_evidence),
            "requested_capabilities": list(self.requested_capabilities),
        }


def apply_patch(
    patch: WorkflowPatch, base: Mapping[str, object], *, new_version: str,
) -> dict[str, object]:
    """Apply the patch to its exact base document and re-validate the result."""
    document = copy.deepcopy(dict(base))
    if document.get("workflow_id") != patch.base.workflow_id:
        raise WorkflowPatchError("patch base workflow_id does not match the workflow")
    if document.get("version") != patch.base.version:
        raise WorkflowPatchError("patch base version does not match the workflow")
    if digest_document(document) != patch.base.digest:
        raise WorkflowPatchError("patch base digest does not match the workflow")
    nodes = cast("list[dict[str, object]]", document["nodes"])
    for operation in patch.operations:
        if operation.op == "add_node":
            _add_node(nodes, operation)
            continue
        target = _node(nodes, operation.node_id)
        if operation.op == "remove_node":
            nodes.remove(target)
        elif operation.op == "replace_constraint":
            _replace_constraint(target, operation)
        elif operation.op == "replace_input":
            _replace_nested(target, operation, container="input")
        else:
            _replace_dependency(target, operation)
    document["version"] = new_version
    try:
        WorkflowValidator().validate(document)
    except WorkflowValidationError as error:
        raise WorkflowPatchError(f"patched workflow is invalid: {error}") from error
    return document


def operation_document(operation: PatchOperation) -> dict[str, object]:
    document: dict[str, object] = {"op": operation.op, "node_id": operation.node_id}
    if operation.path is not None:
        document["path"] = operation.path
    if operation.value is not None:
        document["value"] = operation.value
    return document


def _base(value: object) -> PatchBase:
    if not isinstance(value, Mapping) or set(map(str, value)) != _BASE_FIELDS:
        raise WorkflowPatchError("patch base must define workflow_id, version and digest")
    workflow_id = str(value["workflow_id"])
    version = str(value["version"])
    digest = str(value["digest"])
    if not workflow_id or not version:
        raise WorkflowPatchError("patch base identity must not be empty")
    if _DIGEST.fullmatch(digest) is None:
        raise WorkflowPatchError("patch base digest must be a sha256 digest")
    return PatchBase(workflow_id, version, digest)


def _operations(value: object) -> tuple[PatchOperation, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowPatchError("patch operations must be a non-empty array")
    operations: list[PatchOperation] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise WorkflowPatchError("patch operation must be an object")
        fields = set(map(str, raw))
        unknown = sorted(fields - _OPERATION_FIELDS)
        if unknown:
            raise WorkflowPatchError(f"unexpected operation fields: {', '.join(unknown)}")
        if not {"op", "node_id"} <= fields:
            raise WorkflowPatchError("patch operation requires op and node_id")
        op = str(raw["op"])
        node_id = str(raw["node_id"])
        if op not in _OPS:
            raise WorkflowPatchError(f"unknown patch operation: {op}")
        if not node_id:
            raise WorkflowPatchError("patch operation node_id must not be empty")
        path = None if raw.get("path") is None else str(raw["path"])
        has_value = "value" in raw
        if op == "add_node":
            if path is not None or not has_value or not isinstance(raw["value"], Mapping):
                raise WorkflowPatchError("add_node requires a node document as value")
        elif op == "remove_node":
            if path is not None or has_value:
                raise WorkflowPatchError("remove_node takes neither path nor value")
        else:
            if not path:
                raise WorkflowPatchError(f"{op} requires a path")
            if not has_value:
                raise WorkflowPatchError(f"{op} requires a value")
        operations.append(PatchOperation(
            op, node_id, path, copy.deepcopy(raw.get("value")),
        ))
    return tuple(operations)


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WorkflowPatchError(f"patch {field} must be an array of strings")
    return tuple(value)


def _node(nodes: Sequence[Mapping[str, object]], node_id: str) -> dict[str, object]:
    for node in nodes:
        if str(node.get("node_id")) == node_id:
            return cast("dict[str, object]", node)
    raise WorkflowPatchError(f"patch operation targets unknown node: {node_id}")


def _add_node(nodes: list[dict[str, object]], operation: PatchOperation) -> None:
    added = copy.deepcopy(dict(cast("Mapping[str, object]", operation.value)))
    if str(added.get("node_id")) != operation.node_id:
        raise WorkflowPatchError("added node document must carry the operation node_id")
    if any(str(node.get("node_id")) == operation.node_id for node in nodes):
        raise WorkflowPatchError(f"duplicate node_id after patch: {operation.node_id}")
    nodes.append(added)


def _replace_constraint(node: dict[str, object], operation: PatchOperation) -> None:
    parts = _path(operation, container="constraints")
    field = parts[1]
    if field in _IMMUTABLE_CONSTRAINTS:
        raise WorkflowPatchError(
            f"constraint {field} is not mutable through a workflow patch"
        )
    constraints = cast("dict[str, object]", node["constraints"])
    constraints[field] = operation.value


def _replace_nested(
    node: dict[str, object], operation: PatchOperation, *, container: str,
) -> None:
    parts = _path(operation, container=container)
    target = cast("dict[str, object]", node[container])
    for key in parts[1:-1]:
        nested = target.get(key)
        if not isinstance(nested, dict):
            raise WorkflowPatchError(f"patch path traverses a non-object: {key}")
        target = nested
    target[parts[-1]] = operation.value


def _replace_dependency(node: dict[str, object], operation: PatchOperation) -> None:
    if operation.path != "depends_on":
        raise WorkflowPatchError(
            f"replace_dependency requires the depends_on path: {operation.path}"
        )
    value = operation.value
    if (not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)):
        raise WorkflowPatchError("replace_dependency requires a list of node IDs")
    node["depends_on"] = list(value)


def _path(operation: PatchOperation, *, container: str) -> list[str]:
    parts = (operation.path or "").split(".")
    if parts[0] != container or len(parts) < 2:
        raise WorkflowPatchError(
            f"{operation.op} requires a {container}.* path: {operation.path}"
        )
    return parts
