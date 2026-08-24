"""F04 deterministic CandidatePlan validation before any Skill resolution."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from referencing import Registry, Resource

from active_agent_platform.planning import CandidatePlan
from active_agent_platform.workflow import WorkflowDefinition, WorkflowRegistry, WorkflowStatus


class PlanValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedTask:
    task_id: str
    workflow: WorkflowDefinition
    parameters: Mapping[str, object]
    deadline: datetime
    priority: int


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    plan: CandidatePlan
    tasks: tuple[ValidatedTask, ...]


def _default_schema_path() -> Path:
    relative = Path("schemas/plan/plan-1.0.schema.json")
    candidates = (Path(__file__).parents[1] / relative, Path(sys.prefix) / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"packaged plan schema is missing: {relative}")


class PlanValidator:
    def __init__(self, registry: WorkflowRegistry, *, schema_path: Path | None = None) -> None:
        self._registry = registry
        path = schema_path or _default_schema_path()
        schema = json.loads(path.read_text(encoding="utf-8"))
        dna_path = path.parents[1] / "dna" / "dna-execution-context-1.0.schema.json"
        dna_schema = json.loads(dna_path.read_text(encoding="utf-8"))
        registry = Registry().with_resource(
            str(dna_schema["$id"]), Resource.from_contents(dna_schema),
        )
        self._validator = Draft202012Validator(schema, registry=registry)

    def validate(self, document: Mapping[str, object], *, now: datetime) -> ValidatedPlan:
        try:
            normalized = cast(Mapping[str, object], _plain(document))
            self._validator.validate(normalized)
            plan = CandidatePlan.create(normalized)
        except (ValidationError, ValueError) as error:
            code = getattr(error, "code", "PLAN_SCHEMA_INVALID")
            raise PlanValidationError(str(code), "candidate plan does not match schema") from error
        if now >= plan.expires_at:
            raise PlanValidationError("PLAN_EXPIRED", "candidate plan has expired")
        raw_tasks = cast(list[Mapping[str, object]], normalized["tasks"])
        ids = {str(task["task_id"]) for task in raw_tasks}
        edges: dict[str, tuple[str, ...]] = {}
        tasks: list[ValidatedTask] = []
        for raw in raw_tasks:
            task_id = str(raw["task_id"])
            dependencies = tuple(cast(list[str], raw["depends_on"]))
            if any(item not in ids for item in dependencies):
                raise PlanValidationError("PLAN_GRAPH_INVALID", "task dependency is unknown")
            edges[task_id] = dependencies
            try:
                workflow = self._registry.get(str(raw["workflow_id"]), str(raw["workflow_version"]))
            except ValueError as error:
                raise PlanValidationError("WORKFLOW_NOT_FOUND", "workflow is not registered") from error
            if workflow.status is not WorkflowStatus.ACTIVE:
                raise PlanValidationError("WORKFLOW_NOT_ALLOWED", "workflow version is not active")
            parameters = cast(Mapping[str, object], raw["params"])
            try:
                Draft202012Validator(cast(Mapping[str, object], workflow.definition["input_schema"])).validate(parameters)
            except ValidationError as error:
                raise PlanValidationError("PARAMETER_INVALID", "workflow parameters are invalid") from error
            deadline = datetime.fromisoformat(str(raw["deadline"]))
            if deadline > plan.expires_at or deadline <= now:
                raise PlanValidationError("PLAN_EXPIRED", "task deadline is outside plan validity")
            priority = raw["priority"]
            assert isinstance(priority, int)
            tasks.append(ValidatedTask(task_id, workflow, parameters, deadline, priority))
        _assert_acyclic(edges)
        return ValidatedPlan(plan, tuple(tasks))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _assert_acyclic(edges: Mapping[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise PlanValidationError("PLAN_GRAPH_INVALID", "task graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in edges[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)
