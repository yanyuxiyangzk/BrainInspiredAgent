from __future__ import annotations

from collections.abc import Mapping

import pytest

from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from domain_sdk import (
    CompositionLimits,
    CompositionMode,
    DnaComponent,
    DnaComposer,
    DnaCompositionError,
    DnaDefinition,
    DnaStatus,
)


def schema(**properties: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {name: {"type": kind} for name, kind in properties.items()},
        "required": list(properties),
    }


def child(
    workflow_id: str, version: str, capability: str, input_schema: Mapping[str, object],
    *, status: DnaStatus = DnaStatus.VALIDATED, side_effect: str = "PURE",
    permissions: tuple[str, ...] = (), sub_workflow: tuple[str, str] | None = None,
    timeout: int = 10, parallelism: int = 1,
) -> DnaDefinition:
    if sub_workflow is None:
        nodes: list[dict[str, object]] = [{
            "node_id": "execute", "type": "skill", "depends_on": [],
            "capability": capability, "capability_version": "1.0", "input": {},
            "constraints": {"side_effect": side_effect,
                            "required_permissions": list(permissions)},
        }]
    else:
        nodes = [{
            "node_id": "nested", "type": "sub_workflow", "depends_on": [],
            "workflow_id": sub_workflow[0], "workflow_version": sub_workflow[1],
            "input": {}, "failure_policy": "propagate",
        }]
    return DnaDefinition.from_workflow({
        "spec_version": "1.0", "workflow_id": workflow_id, "version": version,
        "name": workflow_id, "input_schema": dict(input_schema),
        "policy": {"timeout_seconds": timeout, "max_parallelism": parallelism,
                   "required_capabilities": [capability]},
        "nodes": nodes, "output_mapping": {"result": "$.nodes.execute.output.result"}
        if sub_workflow is None else {"result": "$.nodes.nested.output.result"},
    }, status=status)


def components() -> tuple[DnaComponent, DnaComponent]:
    quote = child("market_quote", "1.0.0", "market.quote", schema(symbol="string"))
    summarize = child("market_summary", "1.0.0", "market.summarize", schema(price="number"))
    return (
        DnaComponent("quote", quote, {"symbol": "$.params.symbol"}, schema(price="number")),
        DnaComponent("summary", summarize, {"price": "$.nodes.quote.output.price"},
                     schema(text="string")),
    )


def composer(**changes: object) -> DnaComposer:
    values: dict[str, object] = {
        "allowed_capabilities": frozenset({"market.quote", "market.summarize"}),
        "max_timeout_seconds": 60, "max_parallelism": 4,
    }
    values.update(changes)
    return DnaComposer(CompositionLimits(**values))  # type: ignore[arg-type]


def test_sequential_composition_builds_candidate_with_pinned_parents() -> None:
    quote, summary = components()
    result = composer().compose(
        workflow_id="market_agent", dna_id="agent.market", version="2.0.0",
        name="Market agent", input_schema=schema(symbol="string"),
        components=(quote, summary), output_mapping={"text": "$.nodes.summary.output.text"},
    )
    assert result.status is DnaStatus.CANDIDATE
    assert result.dna_id == "agent.market"
    assert result.generator == {"name": "dna-composer", "version": "1.0",
                                "mode": "SEQUENTIAL"}
    assert [item.version for item in result.parent_dna] == ["1.0.0", "1.0.0"]
    nodes = result.to_document()["workflow"]["nodes"]  # type: ignore[index]
    assert nodes[1]["depends_on"] == ["quote"]
    assert result.workflow["policy"]["timeout_seconds"] == 20  # type: ignore[index]
    registry = WorkflowRegistry()
    for component in (quote, summary):
        registry.register(component.dna.to_document()["workflow"], status=WorkflowStatus.VALIDATED)  # type: ignore[arg-type]
    registered = registry.register(
        result.to_document()["workflow"], status=WorkflowStatus.VALIDATED  # type: ignore[arg-type]
    )
    assert registered.validation.referenced_workflows == (
        ("market_quote", "1.0.0"), ("market_summary", "1.0.0")
    )


def test_parallel_composition_adds_gate_and_accounts_concurrency() -> None:
    quote, _ = components()
    second = DnaComponent(
        "news", child("market_news", "1.0.0", "market.summarize", schema(symbol="string"),
                      parallelism=2),
        {"symbol": "$.params.symbol"}, schema(text="string"),
    )
    result = composer().compose(
        workflow_id="parallel_agent", version="1.0.0", name="Parallel agent",
        input_schema=schema(symbol="string"), components=(quote, second),
        output_mapping={"news": "$.nodes.news.output.text"}, mode=CompositionMode.PARALLEL,
    )
    assert result.workflow["nodes"][0]["branches"] == (("quote",), ("news",))  # type: ignore[index]
    assert result.workflow["nodes"][1]["depends_on"] == ("compose_parallel",)  # type: ignore[index]
    assert result.workflow["policy"]["max_parallelism"] == 3  # type: ignore[index]
    assert result.workflow["policy"]["timeout_seconds"] == 10  # type: ignore[index]


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({}, "misses required"),
        ({"price": "$.params.symbol"}, "incompatible schema"),
        ({"price": "$.nodes.future.output.price"}, "unavailable component"),
        ({"price": "$.bad.path"}, "unsupported JSON path"),
        ({"price": "$.params.missing"}, "unknown schema field"),
        ({"price": 3, "extra": 1}, "maps unknown input"),
    ],
)
def test_composer_rejects_invalid_schema_bindings(
    mapping: dict[str, object], message: str,
) -> None:
    quote, summary = components()
    broken = DnaComponent(summary.alias, summary.dna, mapping, summary.output_schema)
    with pytest.raises(DnaCompositionError, match=message):
        composer().compose(
            workflow_id="invalid_agent", version="1.0.0", name="Invalid",
            input_schema=schema(symbol="string"), components=(quote, broken),
            output_mapping={},
        )


def test_governance_rejects_capability_permission_and_side_effect_escalation() -> None:
    restricted = child(
        "restricted", "1.0.0", "market.trade", schema(symbol="string"),
        side_effect="NON_REPLAYABLE", permissions=("broker.write",),
    )
    component = DnaComponent("trade", restricted, {"symbol": "ABC"}, schema(ok="boolean"))
    common = {"workflow_id": "trade_agent", "version": "1.0.0", "name": "Trade",
              "input_schema": schema(), "components": (component,), "output_mapping": {}}
    with pytest.raises(DnaCompositionError, match="denied capability"):
        composer().compose(**common)
    with pytest.raises(DnaCompositionError, match="denied permission"):
        composer(allowed_capabilities=frozenset({"market.trade"})).compose(**common)
    with pytest.raises(DnaCompositionError, match="side-effect"):
        composer(allowed_capabilities=frozenset({"market.trade"}),
                 allowed_permissions=frozenset({"broker.write"}),
                 max_side_effect="IDEMPOTENT").compose(**common)


def test_budget_status_and_shape_boundaries_are_enforced() -> None:
    quote, summary = components()
    candidate = DnaComponent(
        "candidate", child("candidate_flow", "1.0.0", "market.quote", schema(),
                           status=DnaStatus.CANDIDATE), {}, schema(ok="boolean")
    )
    base = {"workflow_id": "bounded", "version": "1.0.0", "name": "Bounded",
            "input_schema": schema(symbol="string"), "output_mapping": {}}
    with pytest.raises(DnaCompositionError, match="pass validation"):
        composer().compose(components=(candidate,), **base)
    with pytest.raises(DnaCompositionError, match="timeout"):
        composer().compose(components=(quote, summary), timeout_seconds=19, **base)
    with pytest.raises(DnaCompositionError, match="parallelism"):
        composer().compose(components=(quote, summary), max_parallelism=0, **base)
    with pytest.raises(DnaCompositionError, match="at least two"):
        composer().compose(components=(quote,), mode=CompositionMode.PARALLEL, **base)
    with pytest.raises(DnaCompositionError, match="aliases"):
        composer().compose(components=(quote, DnaComponent("quote", summary.dna,
                          summary.input_mapping, summary.output_schema)), **base)


def test_nested_graph_requires_available_acyclic_dna_within_depth_and_node_limits() -> None:
    leaf = child("leaf_flow", "1.0.0", "market.quote", schema())
    middle = child("middle_flow", "1.0.0", "market.quote", schema(),
                   sub_workflow=("leaf_flow", "1.0.0"))
    component = DnaComponent("middle", middle, {}, schema(ok="boolean"))
    common = {"workflow_id": "nested_agent", "version": "1.0.0", "name": "Nested",
              "input_schema": schema(), "components": (component,), "output_mapping": {}}
    with pytest.raises(DnaCompositionError, match="unavailable"):
        composer().compose(**common)
    result = composer().compose(available_dna=(leaf,), **common)
    assert result.parent_dna[0].dna_id == "middle_flow"
    with pytest.raises(DnaCompositionError, match="depth"):
        composer(max_depth=1).compose(available_dna=(leaf,), **common)
    with pytest.raises(DnaCompositionError, match="node count"):
        composer(max_nodes=2).compose(available_dna=(leaf,), **common)

    first = child("cycle_a", "1.0.0", "market.quote", schema(),
                  sub_workflow=("cycle_b", "1.0.0"))
    second = child("cycle_b", "1.0.0", "market.quote", schema(),
                   sub_workflow=("cycle_a", "1.0.0"))
    cyclic = DnaComponent("cycle", first, {}, schema(ok="boolean"))
    with pytest.raises(DnaCompositionError, match="cycle"):
        composer().compose(workflow_id="cycle_agent", version="1.0.0", name="Cycle",
                           input_schema=schema(), components=(cyclic,), output_mapping={},
                           available_dna=(second,))


def test_component_and_limit_contracts_reject_invalid_configuration() -> None:
    definition = child("valid_flow", "1.0.0", "market.quote", schema())
    with pytest.raises(DnaCompositionError, match="alias"):
        DnaComponent("Bad-Alias", definition, {}, schema())
    with pytest.raises(DnaCompositionError, match="object"):
        DnaComponent("valid", definition, {}, {"type": "string"})
    with pytest.raises(DnaCompositionError, match="positive"):
        CompositionLimits(max_depth=0)
    with pytest.raises(DnaCompositionError, match="four parents"):
        CompositionLimits(max_components=5)
    with pytest.raises(DnaCompositionError, match="max_side_effect"):
        CompositionLimits(max_side_effect="UNKNOWN")


def test_json_constants_and_remaining_schema_boundaries_are_checked() -> None:
    constant_schema = schema(nothing="null", flag="boolean", count="number",
                             items="array", metadata="object")
    definition = child("constant_flow", "1.0.0", "market.quote", constant_schema)
    component = DnaComponent(
        "constants", definition,
        {"nothing": None, "flag": True, "count": 3, "items": [], "metadata": {}},
        schema(ok="boolean"),
    )
    result = composer().compose(
        workflow_id="constant_agent", version="1.0.0", name="Constants",
        input_schema=schema(), components=(component,), output_mapping={},
    )
    assert result.status is DnaStatus.CANDIDATE
    with pytest.raises(DnaCompositionError, match="unsupported value"):
        composer().compose(
            workflow_id="bad_constant", version="1.0.0", name="Bad constant",
            input_schema=schema(),
            components=(DnaComponent("constants", definition,
                                     {**component.input_mapping, "count": {1, 2}},
                                     component.output_schema),), output_mapping={},
        )
    with pytest.raises(DnaCompositionError, match="component count"):
        composer().compose(workflow_id="empty_agent", version="1.0.0", name="Empty",
                           input_schema=schema(), components=(), output_mapping={})
    with pytest.raises(DnaCompositionError, match="schema is invalid"):
        composer().compose(
            workflow_id="bad_schema", version="1.0.0", name="Bad schema",
            input_schema={"type": "object", "properties": {"x": {}}, "required": ["x"]},
            components=(component,), output_mapping={},
        )
