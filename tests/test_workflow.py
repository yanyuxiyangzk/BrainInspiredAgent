import copy
from typing import cast

import pytest

from active_agent_platform import (
    ExpressionError,
    WorkflowRegistry,
    WorkflowRegistryError,
    WorkflowStatus,
    WorkflowValidationError,
    WorkflowValidator,
    evaluate_expression,
    parse_expression,
    resolve_json_path,
)


def definition(*, version: str = "1.0.0", nodes: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "spec_version": "1.0",
        "workflow_id": "summary_flow",
        "version": version,
        "name": "Summary Flow",
        "input_schema": {"type": "object"},
        "policy": {
            "timeout_seconds": 60,
            "max_parallelism": 2,
            "required_capabilities": ["report.summary"],
        },
        "nodes": nodes or [
            {
                "node_id": "summarize",
                "type": "skill",
                "depends_on": [],
                "capability": "report.summary",
                "capability_version": "1.0",
                "input": {"text": "$.params.text"},
                "constraints": {"side_effect": "PURE"},
            }
        ],
        "output_mapping": {"summary": "$.nodes.summarize.output"},
    }


def test_registry_registers_validates_and_freezes_digest() -> None:
    registry = WorkflowRegistry()
    raw = definition()
    item = registry.register(raw, status=WorkflowStatus.VALIDATED)
    assert item.status is WorkflowStatus.VALIDATED
    assert item.digest.startswith("sha256:") and len(item.digest) == 71
    assert item.validation.topological_order == ("summarize",)
    with pytest.raises(TypeError):
        item.definition["name"] = "changed"  # type: ignore[index]
    raw["name"] = "changed"
    assert registry.get("summary_flow", "1.0.0").definition["name"] == "Summary Flow"
    with pytest.raises(WorkflowRegistryError):
        registry.register(definition())


def test_registry_activation_is_versioned_and_only_one_active() -> None:
    registry = WorkflowRegistry()
    registry.register(definition(version="1.0.0"), status=WorkflowStatus.VALIDATED)
    registry.register(definition(version="1.1.0"), status=WorkflowStatus.VALIDATED)
    first = registry.activate("summary_flow", "1.0.0")
    assert registry.active("summary_flow") is first
    second = registry.activate("summary_flow", "1.1.0")
    assert second.status is WorkflowStatus.ACTIVE
    assert registry.get("summary_flow", "1.0.0").status is WorkflowStatus.DEPRECATED
    assert registry.active("summary_flow").version == "1.1.0"
    assert registry.activate("summary_flow", "1.1.0") is second
    with pytest.raises(WorkflowRegistryError):
        registry.activate("missing", "1.0.0")
    with pytest.raises(WorkflowRegistryError):
        registry.active("missing")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("name"),
        lambda value: value.update(spec_version="2.0"),
        lambda value: value.update(workflow_id="Bad-ID"),
        lambda value: value.update(version="1"),
        lambda value: value.update(policy={"timeout_seconds": 0, "max_parallelism": 1, "required_capabilities": []}),
        lambda value: value.update(nodes=[]),
        lambda value: value.update(output_mapping={"x": "$.nodes.missing.output"}),
    ],
)
def test_validator_rejects_invalid_workflow_contracts(mutate: object) -> None:
    value = definition()
    mutate_fn = mutate
    mutate_fn(value)  # type: ignore[operator]
    with pytest.raises(WorkflowValidationError):
        WorkflowValidator().validate(value)


def test_validator_rejects_duplicate_ids_unknown_dependencies_and_unreachable_nodes() -> None:
    base = definition()
    nodes = base["nodes"]
    assert isinstance(nodes, list)
    duplicate = copy.deepcopy(nodes[0])
    nodes.append(duplicate)
    with pytest.raises(WorkflowValidationError):
        WorkflowValidator().validate(base)

    invalid = definition(nodes=[
        {"node_id": "a", "type": "skill", "depends_on": ["missing"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
    ])
    with pytest.raises(WorkflowValidationError):
        WorkflowValidator().validate(invalid)

    unreachable = definition(nodes=[
        {"node_id": "a", "type": "skill", "depends_on": [], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
        {"node_id": "b", "type": "skill", "depends_on": [], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
    ])
    # Independent roots are valid DAG entry points, so explicitly make b unreachable via a cycle.
    unreachable["nodes"][1]["depends_on"] = ["b"]  # type: ignore[index]
    with pytest.raises(WorkflowValidationError):
        WorkflowValidator().validate(unreachable)


def test_validator_checks_branch_edges_cycles_and_subworkflow_references() -> None:
    branch = definition(nodes=[
        {"node_id": "choose", "type": "condition", "depends_on": [], "expression": "$.params.ok == true", "then": ["yes"], "else": ["no"]},
        {"node_id": "yes", "type": "skill", "depends_on": ["choose"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
        {"node_id": "no", "type": "skill", "depends_on": ["choose"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
    ])
    branch["output_mapping"] = {"summary": "$.nodes.yes.output"}
    assert WorkflowValidator().validate(branch).topological_order == ("choose", "no", "yes")
    bad_branch = copy.deepcopy(branch)
    bad_branch["nodes"][0]["then"] = ["missing"]  # type: ignore[index]
    with pytest.raises(WorkflowValidationError):
        WorkflowValidator().validate(bad_branch)

    cycle = definition(nodes=[
        {"node_id": "a", "type": "skill", "depends_on": ["b"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
        {"node_id": "b", "type": "skill", "depends_on": ["a"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
    ])
    with pytest.raises(WorkflowValidationError, match="cycle"):
        WorkflowValidator().validate(cycle)

    child = {"node_id": "child", "type": "sub_workflow", "depends_on": [], "workflow_id": "summary_flow", "workflow_version": "1.0.0", "input": {}, "failure_policy": "propagate"}
    with pytest.raises(WorkflowValidationError, match="directly"):
        WorkflowValidator().validate(definition(nodes=[cast(dict[str, object], child)]))


def test_safe_json_path_and_comparison_expression() -> None:
    context = {"params": {"ok": True, "score": 12, "name": "alpha"}}
    assert resolve_json_path(context, "$.params.score") == (True, 12)
    assert resolve_json_path(context, "$.params.missing") == (False, None)
    assert evaluate_expression("$.params.ok == true", context)
    assert evaluate_expression("$.params.score >= 10", context)
    assert evaluate_expression("$.params.name != 'beta'", context)
    assert not evaluate_expression("$.params.missing == true", context)
    assert parse_expression("$.params.score < 20") == ("$.params.score", "<", 20)
    for expression in ("params.score == 1", "$.params[0] == 1", "$.params.score + 1 == 2", "$.params.score == foo", "__import__('os')"):
        with pytest.raises(ExpressionError):
            parse_expression(expression)
    with pytest.raises(ExpressionError):
        resolve_json_path(context, "$.params[0]")


def test_validator_rejects_invalid_node_types_and_controls() -> None:
    for node in [
        {"node_id": "x", "type": "unknown", "depends_on": []},
        {"node_id": "x", "type": "delay", "depends_on": [], "duration_seconds": 0},
        {"node_id": "x", "type": "delay", "depends_on": [], "duration_seconds": 1, "until": "2026-01-01"},
        {"node_id": "x", "type": "parallel", "depends_on": [], "branches": [["x"]], "failure_policy": "fail_fast"},
        {"node_id": "x", "type": "condition", "depends_on": [], "expression": "eval('x')", "then": [], "else": []},
        {"node_id": "x", "type": "sub_workflow", "depends_on": [], "workflow_id": "child", "workflow_version": "bad", "input": {}, "failure_policy": "propagate"},
    ]:
        with pytest.raises(WorkflowValidationError):
            WorkflowValidator().validate(definition(nodes=[cast(dict[str, object], node)]))


def test_registry_rejects_direct_active_registration_and_duplicate_subworkflow() -> None:
    registry = WorkflowRegistry()
    with pytest.raises(WorkflowRegistryError):
        registry.register(definition(), status=WorkflowStatus.ACTIVE)
    child = definition()
    child["workflow_id"] = "child_flow"
    registry.register(child, status=WorkflowStatus.VALIDATED)
    parent_node = {
        "node_id": "child",
        "type": "sub_workflow",
        "depends_on": [],
        "workflow_id": "child_flow",
        "workflow_version": "1.0.0",
        "input": {},
        "failure_policy": "propagate",
    }
    parent = definition()
    parent["workflow_id"] = "parent_flow"
    parent["output_mapping"] = {}
    parent["nodes"] = [parent_node]
    validated = registry.register(parent, status=WorkflowStatus.VALIDATED)
    assert validated.validation.referenced_workflows == (("child_flow", "1.0.0"),)


def test_validator_accepts_parallel_delay_and_compensation_controls() -> None:
    nodes: list[dict[str, object]] = [
        {"node_id": "start", "type": "delay", "depends_on": [], "duration_seconds": 1},
        {"node_id": "left", "type": "skill", "depends_on": ["start"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
        {"node_id": "right", "type": "skill", "depends_on": ["start"], "capability": "report.summary", "capability_version": "1.0", "input": {}, "constraints": {"side_effect": "PURE"}},
        {"node_id": "join", "type": "parallel", "depends_on": ["start"], "branches": [["left"], ["right"]], "failure_policy": "min_success", "min_success": 1},
        {"node_id": "finish", "type": "sub_workflow", "depends_on": ["join"], "workflow_id": "child_flow", "workflow_version": "1.0.0", "input": {}, "failure_policy": "compensate", "compensation_node": "left"},
    ]
    value = definition(nodes=nodes)
    value["workflow_id"] = "parent_flow"
    value["output_mapping"] = {}
    known = {("child_flow", "1.0.0"): definition()}
    result = WorkflowValidator().validate(value, known_workflows=known)
    assert result.topological_order[0] == "start"
