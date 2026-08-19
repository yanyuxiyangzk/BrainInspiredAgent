"""Versioned Workflow definitions, static validation, DAG checks and safe expressions."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import cast


class WorkflowStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class WorkflowValidationError(ValueError):
    pass


class WorkflowRegistryError(ValueError):
    pass


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowValidation:
    workflow_id: str
    version: str
    digest: str
    topological_order: tuple[str, ...]
    referenced_workflows: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: str
    version: str
    status: WorkflowStatus
    digest: str
    definition: Mapping[str, object]
    validation: WorkflowValidation

    def __post_init__(self) -> None:
        object.__setattr__(self, "definition", _freeze(self.definition))


class WorkflowValidator:
    """Perform schema-like structural checks without executing user supplied code."""

    _ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    _VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
    _CAPABILITY_VERSION = re.compile(r"^[1-9][0-9]*\.[0-9]+$")
    _CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
    _NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    _TYPES = frozenset({"skill", "condition", "parallel", "delay", "sub_workflow"})

    def __init__(self, *, max_nodes: int = 100, max_depth: int = 8) -> None:
        if max_nodes < 1 or max_depth < 1:
            raise ValueError("workflow limits must be positive")
        self._max_nodes = max_nodes
        self._max_depth = max_depth

    def validate(
        self,
        definition: Mapping[str, object],
        *,
        known_workflows: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    ) -> WorkflowValidation:
        if not isinstance(definition, Mapping):
            raise WorkflowValidationError("workflow must be an object")
        self._required(definition, ("spec_version", "workflow_id", "version", "name", "input_schema", "policy", "nodes", "output_mapping"))
        if definition["spec_version"] != "1.0":
            raise WorkflowValidationError("unsupported workflow spec_version")
        workflow_id = _string(definition, "workflow_id")
        version = _string(definition, "version")
        if self._ID.fullmatch(workflow_id) is None or self._VERSION.fullmatch(version) is None:
            raise WorkflowValidationError("invalid workflow ID or version")
        if not isinstance(definition["name"], str) or not definition["name"]:
            raise WorkflowValidationError("workflow name must be non-empty")
        if not isinstance(definition["input_schema"], Mapping) or not isinstance(definition["output_mapping"], Mapping):
            raise WorkflowValidationError("input_schema and output_mapping must be objects")
        policy = definition["policy"]
        if not isinstance(policy, Mapping):
            raise WorkflowValidationError("policy must be an object")
        self._required(policy, ("timeout_seconds", "max_parallelism", "required_capabilities"))
        if not _integer_range(policy["timeout_seconds"], 1, 3600) or not _integer_range(policy["max_parallelism"], 1, 32):
            raise WorkflowValidationError("invalid workflow policy limits")
        capabilities = policy["required_capabilities"]
        if not _unique_strings(capabilities, self._CAPABILITY):
            raise WorkflowValidationError("required_capabilities must be unique valid identifiers")
        capability_names = cast(list[str], capabilities)
        raw_nodes = definition["nodes"]
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= self._max_nodes:
            raise WorkflowValidationError("nodes must contain between 1 and max_nodes items")
        nodes = {self._validate_node(node, capability_names) for node in raw_nodes}
        if len(nodes) != len(raw_nodes):
            raise WorkflowValidationError("node IDs must be unique")
        node_map = {node["node_id"]: node for node in raw_nodes if isinstance(node, Mapping)}
        edges: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        referenced: set[tuple[str, str]] = set()
        for node_id, node in node_map.items():
            for dependency in node["depends_on"]:
                if dependency not in nodes:
                    raise WorkflowValidationError(f"node {node_id} depends on unknown node {dependency}")
                edges[dependency].add(node_id)
            for target in _branch_targets(node):
                if target not in nodes:
                    raise WorkflowValidationError(f"node {node_id} references unknown branch node {target}")
                edges[node_id].add(target)
            if node["type"] == "sub_workflow":
                child = (node["workflow_id"], node["workflow_version"])
                referenced.add(child)
                if child == (workflow_id, version):
                    raise WorkflowValidationError("workflow cannot directly reference itself")
                if known_workflows is not None and child not in known_workflows:
                    raise WorkflowValidationError(f"unknown sub-workflow: {child[0]}@{child[1]}")
        order = _topological_order(nodes, edges)
        _validate_output_mapping(definition["output_mapping"], nodes)
        digest = _digest(definition)
        return WorkflowValidation(workflow_id, version, digest, order, tuple(sorted(referenced)))

    def _validate_node(self, node: object, capabilities: list[str]) -> str:
        if not isinstance(node, Mapping):
            raise WorkflowValidationError("every node must be an object")
        self._required(node, ("node_id", "type", "depends_on"))
        node_id = node["node_id"]
        node_type = node["type"]
        if not isinstance(node_id, str) or self._NODE_ID.fullmatch(node_id) is None:
            raise WorkflowValidationError("invalid node_id")
        if node_type not in self._TYPES or not isinstance(node["depends_on"], list) or not _unique_strings(node["depends_on"], self._NODE_ID):
            raise WorkflowValidationError(f"invalid node {node_id} type or dependencies")
        if "timeout_seconds" in node and not _integer_range(node["timeout_seconds"], 1, 300):
            raise WorkflowValidationError(f"invalid timeout for node {node_id}")
        if node_type == "skill":
            self._required(node, ("capability", "capability_version", "input", "constraints"))
            if not isinstance(node["capability"], str) or self._CAPABILITY.fullmatch(node["capability"]) is None:
                raise WorkflowValidationError(f"invalid capability for node {node_id}")
            if node["capability"] not in capabilities or not isinstance(node["capability_version"], str) or self._CAPABILITY_VERSION.fullmatch(node["capability_version"]) is None:
                raise WorkflowValidationError(f"skill capability is not declared by policy: {node_id}")
            if not isinstance(node["input"], Mapping) or not isinstance(node["constraints"], Mapping) or node["constraints"].get("side_effect") not in {"PURE", "IDEMPOTENT", "QUERYABLE", "NON_REPLAYABLE"}:
                raise WorkflowValidationError(f"invalid skill contract for node {node_id}")
        elif node_type == "condition":
            self._required(node, ("expression", "then", "else"))
            if not isinstance(node["expression"], str):
                raise WorkflowValidationError(f"invalid condition expression for node {node_id}")
            try:
                parse_expression(node["expression"])
            except ExpressionError as error:
                raise WorkflowValidationError(f"invalid condition expression for node {node_id}") from error
            if not _unique_strings(node["then"], self._NODE_ID) or not _unique_strings(node["else"], self._NODE_ID):
                raise WorkflowValidationError(f"invalid condition branch for node {node_id}")
        elif node_type == "parallel":
            self._required(node, ("branches", "failure_policy"))
            if node["failure_policy"] not in {"fail_fast", "collect_all", "min_success"} or not isinstance(node["branches"], list) or len(node["branches"]) < 2:
                raise WorkflowValidationError(f"invalid parallel node {node_id}")
            if node["failure_policy"] == "min_success" and not _integer_range(node.get("min_success"), 1, len(node["branches"])):
                raise WorkflowValidationError(f"invalid min_success for node {node_id}")
            if any(not _unique_strings(branch, self._NODE_ID) for branch in node["branches"]):
                raise WorkflowValidationError(f"invalid parallel branch for node {node_id}")
        elif node_type == "delay":
            has_duration = "duration_seconds" in node
            has_until = "until" in node
            if has_duration == has_until or has_duration and (not isinstance(node["duration_seconds"], int | float) or not 0 < node["duration_seconds"] <= 60) or has_until and not isinstance(node["until"], str):
                raise WorkflowValidationError(f"invalid delay node {node_id}")
        else:
            self._required(node, ("workflow_id", "workflow_version", "input", "failure_policy"))
            if not isinstance(node["workflow_id"], str) or self._ID.fullmatch(node["workflow_id"]) is None or not isinstance(node["workflow_version"], str) or self._VERSION.fullmatch(node["workflow_version"]) is None or not isinstance(node["input"], Mapping) or node["failure_policy"] not in {"propagate", "continue", "compensate"}:
                raise WorkflowValidationError(f"invalid sub-workflow node {node_id}")
            if node["failure_policy"] == "compensate" and not isinstance(node.get("compensation_node"), str):
                raise WorkflowValidationError(f"compensation node required for {node_id}")
        return node_id

    @staticmethod
    def _required(value: Mapping[str, object], fields: tuple[str, ...]) -> None:
        missing = [field for field in fields if field not in value]
        if missing:
            raise WorkflowValidationError(f"missing required fields: {', '.join(missing)}")


class WorkflowRegistry:
    """Immutable-version registry; activation changes only the registry projection."""

    def __init__(self, validator: WorkflowValidator | None = None) -> None:
        self._validator = validator or WorkflowValidator()
        self._definitions: dict[tuple[str, str], WorkflowDefinition] = {}
        self._active: dict[str, tuple[str, str]] = {}

    def register(self, definition: Mapping[str, object], *, status: WorkflowStatus = WorkflowStatus.DRAFT) -> WorkflowDefinition:
        validation = self._validator.validate(definition, known_workflows={key: item.definition for key, item in self._definitions.items()})
        key = (validation.workflow_id, validation.version)
        if key in self._definitions:
            raise WorkflowRegistryError(f"workflow version already registered: {key[0]}@{key[1]}")
        if status is WorkflowStatus.ACTIVE:
            raise WorkflowRegistryError("new definitions must be activated explicitly")
        item = WorkflowDefinition(validation.workflow_id, validation.version, status, validation.digest, definition, validation)
        self._definitions[key] = item
        return item

    def activate(self, workflow_id: str, version: str) -> WorkflowDefinition:
        key = (workflow_id, version)
        item = self._definitions.get(key)
        if item is None:
            raise WorkflowRegistryError(f"workflow not found: {workflow_id}@{version}")
        if item.status is WorkflowStatus.ACTIVE:
            return item
        previous_key = self._active.get(workflow_id)
        if previous_key is not None:
            previous = self._definitions[previous_key]
            self._definitions[previous_key] = WorkflowDefinition(previous.workflow_id, previous.version, WorkflowStatus.DEPRECATED, previous.digest, previous.definition, previous.validation)
        updated = WorkflowDefinition(item.workflow_id, item.version, WorkflowStatus.ACTIVE, item.digest, item.definition, item.validation)
        self._definitions[key] = updated
        self._active[workflow_id] = key
        return updated

    def get(self, workflow_id: str, version: str) -> WorkflowDefinition:
        try:
            return self._definitions[(workflow_id, version)]
        except KeyError as error:
            raise WorkflowRegistryError(f"workflow not found: {workflow_id}@{version}") from error

    def active(self, workflow_id: str) -> WorkflowDefinition:
        key = self._active.get(workflow_id)
        if key is None:
            raise WorkflowRegistryError(f"no active workflow: {workflow_id}")
        return self._definitions[key]

    def all(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._definitions.values())


_EXPRESSION = re.compile(r"^(\$\.[a-zA-Z_][a-zA-Z0-9_.]*)\s*(==|!=|>=|<=|>|<)\s*(.+)$")


def parse_expression(expression: str) -> tuple[str, str, object]:
    match = _EXPRESSION.fullmatch(expression.strip())
    if match is None:
        raise ExpressionError("expression must be '$.path OP literal'")
    path, operator, literal = match.groups()
    value = _parse_literal(literal.strip())
    return path, operator, value


def evaluate_expression(expression: str, context: Mapping[str, object]) -> bool:
    path, operator, expected = parse_expression(expression)
    found, actual = resolve_json_path(context, path)
    if not found:
        return False
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if isinstance(actual, bool) or isinstance(expected, bool) or not isinstance(actual, int | float) or not isinstance(expected, int | float):
        return False
    return {">": actual > expected, ">=": actual >= expected, "<": actual < expected, "<=": actual <= expected}[operator]


def resolve_json_path(context: Mapping[str, object], path: str) -> tuple[bool, object]:
    if not re.fullmatch(r"\$\.[a-zA-Z_][a-zA-Z0-9_.]*", path):
        raise ExpressionError("only dotted object paths are allowed")
    current: object = context
    for part in path[2:].split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _parse_literal(value: str) -> object:
    if value in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[value]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    try:
        number = float(value) if "." in value else int(value)
    except ValueError as error:
        raise ExpressionError("right-hand side must be a JSON scalar literal") from error
    if isinstance(number, float) and not isfinite(number):
        raise ExpressionError("numeric literal must be finite")
    return number


def _branch_targets(node: Mapping[str, object]) -> tuple[str, ...]:
    if node["type"] == "condition":
        return tuple(cast(list[str], node["then"])) + tuple(cast(list[str], node["else"]))
    if node["type"] == "parallel":
        branches = cast(list[list[str]], node["branches"])
        return tuple(target for branch in branches for target in branch)
    if node["type"] == "sub_workflow" and isinstance(node.get("compensation_node"), str):
        return (cast(str, node["compensation_node"]),)
    return ()


def _topological_order(nodes: set[str], edges: Mapping[str, set[str]]) -> tuple[str, ...]:
    incoming = {node: 0 for node in nodes}
    for targets in edges.values():
        for target in targets:
            incoming[target] += 1
    ready = sorted(node for node, count in incoming.items() if count == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in sorted(edges[node]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(nodes):
        raise WorkflowValidationError("workflow graph contains a cycle")
    return tuple(order)


def _validate_output_mapping(mapping: Mapping[str, object], nodes: set[str]) -> None:
    for value in mapping.values():
        if isinstance(value, str) and value.startswith("$.nodes."):
            parts = value.split(".")
            if len(parts) < 3 or parts[2] not in nodes:
                raise WorkflowValidationError("output_mapping references unknown node")


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    if not isinstance(value, str):
        raise WorkflowValidationError(f"{key} must be a string")
    return value


def _integer_range(value: object, lower: int, upper: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lower <= value <= upper


def _unique_strings(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, list) and len(value) == len(set(value)) and all(isinstance(item, str) and pattern.fullmatch(item) for item in value)
