"""Statically governed composition of Workflow DNA into executable parent DNA."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from active_agent_platform.workflow import WorkflowValidator
from domain_sdk.dna import DnaDefinition, DnaError, DnaParent, DnaStatus


class CompositionMode(StrEnum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


class DnaCompositionError(DnaError):
    pass


@dataclass(frozen=True, slots=True)
class DnaComponent:
    alias: str
    dna: DnaDefinition
    input_mapping: Mapping[str, object]
    output_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,47}", self.alias) is None:
            raise DnaCompositionError("component alias is invalid")
        _object_schema(self.output_schema, "component output")


@dataclass(frozen=True, slots=True)
class CompositionLimits:
    max_components: int = 4
    max_nodes: int = 100
    max_depth: int = 8
    max_timeout_seconds: int = 3600
    max_parallelism: int = 16
    allowed_capabilities: frozenset[str] | None = None
    allowed_permissions: frozenset[str] = frozenset()
    max_side_effect: str = "QUERYABLE"

    def __post_init__(self) -> None:
        if min(self.max_components, self.max_nodes, self.max_depth,
               self.max_timeout_seconds, self.max_parallelism) < 1:
            raise DnaCompositionError("composition limits must be positive")
        if self.max_components > 4:
            raise DnaCompositionError("DNA Registry supports at most four parents")
        if self.max_side_effect not in _SIDE_EFFECT_RISK:
            raise DnaCompositionError("max_side_effect is invalid")


_COMPOSABLE = frozenset({DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY,
                         DnaStatus.ACTIVE, DnaStatus.DEPRECATED})
_SIDE_EFFECT_RISK = {"PURE": 0, "IDEMPOTENT": 1, "QUERYABLE": 2, "NON_REPLAYABLE": 3}


class DnaComposer:
    """Build a candidate parent without changing or activating any source DNA."""

    def __init__(self, limits: CompositionLimits | None = None) -> None:
        self._limits = limits or CompositionLimits()

    def compose(
        self,
        *,
        workflow_id: str,
        version: str,
        name: str,
        input_schema: Mapping[str, object],
        components: Sequence[DnaComponent],
        output_mapping: Mapping[str, object],
        mode: CompositionMode = CompositionMode.SEQUENTIAL,
        dna_id: str | None = None,
        available_dna: Iterable[DnaDefinition] = (),
        timeout_seconds: int | None = None,
        max_parallelism: int | None = None,
    ) -> DnaDefinition:
        if not 1 <= len(components) <= self._limits.max_components:
            raise DnaCompositionError("component count exceeds composition limits")
        if mode is CompositionMode.PARALLEL and len(components) < 2:
            raise DnaCompositionError("parallel composition requires at least two components")
        aliases = [component.alias for component in components]
        if len(aliases) != len(set(aliases)):
            raise DnaCompositionError("component aliases must be unique")
        parent_input = _object_schema(input_schema, "composition input")
        catalog = {(item.workflow_validation.workflow_id, item.version): item
                   for item in (*components_dna(components), *tuple(available_dna))}
        capabilities: set[str] = set()
        permissions: set[str] = set()
        side_effect = "PURE"
        child_timeouts: list[int] = []
        child_parallelism: list[int] = []
        outputs: dict[str, Mapping[str, object]] = {}
        for component in components:
            if component.dna.status not in _COMPOSABLE:
                raise DnaCompositionError(
                    f"component must pass validation before composition: {component.alias}"
                )
            self._validate_input(component, parent_input, outputs)
            child_workflow = component.dna.workflow
            policy = cast(Mapping[str, object], child_workflow["policy"])
            capabilities.update(cast(Sequence[str], policy["required_capabilities"]))
            child_timeouts.append(cast(int, policy["timeout_seconds"]))
            child_parallelism.append(cast(int, policy["max_parallelism"]))
            for node in cast(Sequence[Mapping[str, object]], child_workflow["nodes"]):
                if node["type"] != "skill":
                    continue
                constraints = cast(Mapping[str, object], node["constraints"])
                permissions.update(cast(Sequence[str], constraints.get("required_permissions", ())))
                candidate = cast(str, constraints["side_effect"])
                if _SIDE_EFFECT_RISK[candidate] > _SIDE_EFFECT_RISK[side_effect]:
                    side_effect = candidate
            outputs[component.alias] = component.output_schema
        self._validate_governance(capabilities, permissions, side_effect)
        depth, expanded_nodes = _graph_metrics(catalog, tuple(catalog_key(item.dna)
                                                              for item in components))
        if depth > self._limits.max_depth:
            raise DnaCompositionError("composition exceeds maximum sub-DNA depth")
        own_nodes = len(components) + (1 if mode is CompositionMode.PARALLEL else 0)
        if expanded_nodes + own_nodes > self._limits.max_nodes:
            raise DnaCompositionError("composition exceeds maximum expanded node count")
        required_timeout = (sum(child_timeouts) if mode is CompositionMode.SEQUENTIAL
                            else max(child_timeouts))
        timeout = required_timeout if timeout_seconds is None else timeout_seconds
        if timeout < required_timeout or timeout > self._limits.max_timeout_seconds:
            raise DnaCompositionError("composition timeout does not cover child budget")
        required_parallelism = (max(child_parallelism) if mode is CompositionMode.SEQUENTIAL
                                else sum(child_parallelism))
        parallelism = required_parallelism if max_parallelism is None else max_parallelism
        if parallelism < required_parallelism or parallelism > self._limits.max_parallelism:
            raise DnaCompositionError("composition parallelism does not cover child budget")
        nodes = _nodes(components, mode)
        known = {key: item.workflow for key, item in catalog.items()}
        workflow: dict[str, object] = {
            "spec_version": "1.0", "workflow_id": workflow_id, "version": version,
            "name": name, "input_schema": dict(input_schema),
            "policy": {"timeout_seconds": timeout, "max_parallelism": parallelism,
                       "required_capabilities": sorted(capabilities)},
            "nodes": nodes, "output_mapping": dict(output_mapping),
        }
        WorkflowValidator(max_nodes=self._limits.max_nodes,
                          max_depth=self._limits.max_depth).validate(
                              workflow, known_workflows=known
                          )
        return DnaDefinition.from_workflow(
            workflow, dna_id=dna_id, version=version,
            parent_dna=tuple(DnaParent(item.dna.dna_id, item.dna.version,
                                      item.dna.content_digest) for item in components),
            generator={"name": "dna-composer", "version": "1.0", "mode": mode.value},
        )

    def _validate_input(
        self, component: DnaComponent, parent_input: Mapping[str, object],
        outputs: Mapping[str, Mapping[str, object]],
    ) -> None:
        target = _object_schema(
            cast(Mapping[str, object], component.dna.workflow["input_schema"]),
            f"{component.alias} input",
        )
        properties = cast(Mapping[str, object], target.get("properties", {}))
        required = cast(Sequence[str], target.get("required", ()))
        if not set(required) <= set(component.input_mapping):
            raise DnaCompositionError(f"component {component.alias} misses required input")
        if not set(component.input_mapping) <= set(properties):
            raise DnaCompositionError(f"component {component.alias} maps unknown input")
        for name, value in component.input_mapping.items():
            rule = cast(Mapping[str, object], properties[name])
            source_type = _source_type(value, parent_input, outputs)
            if source_type is not None and source_type != rule.get("type"):
                raise DnaCompositionError(
                    f"component {component.alias} input {name} has incompatible schema"
                )

    def _validate_governance(
        self, capabilities: set[str], permissions: set[str], side_effect: str,
    ) -> None:
        allowed = self._limits.allowed_capabilities
        if allowed is not None and not capabilities <= allowed:
            raise DnaCompositionError("composition requests a denied capability")
        if not permissions <= self._limits.allowed_permissions:
            raise DnaCompositionError("composition requests a denied permission")
        if _SIDE_EFFECT_RISK[side_effect] > _SIDE_EFFECT_RISK[self._limits.max_side_effect]:
            raise DnaCompositionError("composition exceeds side-effect boundary")


def components_dna(components: Sequence[DnaComponent]) -> tuple[DnaDefinition, ...]:
    return tuple(item.dna for item in components)


def catalog_key(dna: DnaDefinition) -> tuple[str, str]:
    return dna.workflow_validation.workflow_id, dna.version


def _nodes(components: Sequence[DnaComponent], mode: CompositionMode) -> list[dict[str, object]]:
    gate = "compose_parallel"
    nodes: list[dict[str, object]] = []
    if mode is CompositionMode.PARALLEL:
        nodes.append({"node_id": gate, "type": "parallel", "depends_on": [],
                      "branches": [[item.alias] for item in components],
                      "failure_policy": "fail_fast"})
    previous: str | None = None
    for component in components:
        depends = ([previous] if previous is not None else [])
        nodes.append({
            "node_id": component.alias, "type": "sub_workflow",
            "depends_on": [gate] if mode is CompositionMode.PARALLEL else depends,
            "workflow_id": component.dna.workflow_validation.workflow_id,
            "workflow_version": component.dna.version,
            "input": dict(component.input_mapping), "failure_policy": "propagate",
        })
        previous = component.alias
    return nodes


def _object_schema(schema: Mapping[str, object], label: str) -> Mapping[str, object]:
    if schema.get("type") != "object":
        raise DnaCompositionError(f"{label} schema must be an object")
    properties, required = schema.get("properties", {}), schema.get("required", ())
    if (not isinstance(properties, Mapping) or not isinstance(required, Sequence)
            or isinstance(required, str | bytes)
            or any(not isinstance(name, str) or name not in properties for name in required)
            or any(not isinstance(rule, Mapping) or "type" not in rule
                   for rule in properties.values())):
        raise DnaCompositionError(f"{label} schema is invalid")
    return schema


def _source_type(
    value: object, parent: Mapping[str, object], outputs: Mapping[str, Mapping[str, object]],
) -> str | None:
    if not isinstance(value, str) or not value.startswith("$."):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, str):
            return "string"
        if isinstance(value, int | float):
            return "number"
        if isinstance(value, list):
            return "array"
        if isinstance(value, Mapping):
            return "object"
        raise DnaCompositionError("composition input contains an unsupported value")
    parts = value.split(".")
    if len(parts) == 3 and parts[1] == "params":
        schema = parent
        name = parts[2]
    elif len(parts) == 5 and parts[1] == "nodes" and parts[3] == "output":
        if parts[2] not in outputs:
            raise DnaCompositionError("composition input references unavailable component output")
        schema, name = outputs[parts[2]], parts[4]
    else:
        raise DnaCompositionError("composition input uses an unsupported JSON path")
    properties = cast(Mapping[str, object], schema.get("properties", {}))
    if name not in properties:
        raise DnaCompositionError("composition input references an unknown schema field")
    return cast(Mapping[str, object], properties[name]).get("type")  # type: ignore[return-value]


def _graph_metrics(
    catalog: Mapping[tuple[str, str], DnaDefinition], roots: tuple[tuple[str, str], ...],
) -> tuple[int, int]:
    def visit(key: tuple[str, str], path: frozenset[tuple[str, str]]) -> tuple[int, int]:
        if key in path:
            raise DnaCompositionError("sub-DNA graph contains a cycle")
        dna = catalog.get(key)
        if dna is None:
            raise DnaCompositionError(f"referenced sub-DNA is unavailable: {key[0]}@{key[1]}")
        workflow = dna.workflow
        children = tuple((cast(str, node["workflow_id"]), cast(str, node["workflow_version"]))
                         for node in cast(Sequence[Mapping[str, object]], workflow["nodes"])
                         if node["type"] == "sub_workflow")
        own = len(cast(Sequence[object], workflow["nodes"]))
        if not children:
            return 1, own
        metrics = [visit(child, path | {key}) for child in children]
        return 1 + max(depth for depth, _ in metrics), own + sum(count for _, count in metrics)

    metrics = [visit(root, frozenset()) for root in roots]
    return max(depth for depth, _ in metrics), sum(count for _, count in metrics)
