"""E05 evolution driver: fitness weaknesses → hypothesis and operations.

Reads the baseline DNA's fitness snapshot, identifies the dominant
weakness, and emits a hypothesis plus a set of governed candidate
operations. The rule engine is deterministic and always available; an
optional structured model (``StructuredModel`` protocol, same seam the
RulePlanner uses) can refine the hypothesis text and parameters. Every
emitted operation still passes through ``DnaCandidateGenerator`` policy
checks, so the LLM cannot widen the governed boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.dna import DnaDefinition
from domain_sdk.dna_candidates import (
    CandidateOperation,
    CandidateOperationKind,
)
from domain_sdk.dna_fitness import FitnessReadiness


class EvolutionDriverError(ValueError):
    pass


class StructuredModel(Protocol):
    async def generate(self, request: Mapping[str, object]) -> str | Mapping[str, object]: ...


WEAKNESS_THRESHOLDS: Mapping[str, float] = {
    "success_rate": 0.95,
    "evidence_score": 0.80,
    "user_value_score": 0.70,
    "stability_rate": 0.95,
}


@dataclass(frozen=True, slots=True)
class EvolutionPlan:
    """A proposed evolution step: weakness, hypothesis and governed operations."""

    weakness: str
    hypothesis: str
    operations: tuple[CandidateOperation, ...]
    source: str
    baseline_version: str
    new_version: str
    snapshot_revision: int

    def operations_document(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {"kind": item.kind.value, "node_id": item.node_id,
             "field": item.field, "value": item.value}
            for item in self.operations
        )


def detect_weakness(snapshot: Mapping[str, object]) -> str | None:
    """Return the dominant weakness key, or None when nothing is below target."""
    ranked = sorted(
        WEAKNESS_THRESHOLDS.items(),
        key=lambda item: float(snapshot[item[0]]) - item[1],  # type: ignore[arg-type]
    )
    for key, threshold in ranked:
        if float(snapshot[key]) < threshold:  # type: ignore[arg-type]
            return key
    return None


class RuleEvolutionStrategy:
    """Deterministic weakness→operations mapping; the always-available fallback."""

    def hypothesis(self, weakness: str, snapshot: Mapping[str, object]) -> str:
        observed = float(str(snapshot[weakness]))
        return (
            f"Raise {weakness} from {observed:.3f} above the {WEAKNESS_THRESHOLDS[weakness]:.2f} "
            "target by tightening the summary input within the governed mutable boundary."
        )

    def operations(
        self, weakness: str, snapshot: Mapping[str, object], baseline: DnaDefinition,
    ) -> tuple[CandidateOperation, ...]:
        nodes = cast("Sequence[Mapping[str, object]]", baseline.workflow["nodes"])
        skill_ids = [str(node["node_id"]) for node in nodes
                     if str(node.get("type")) == "skill"]
        if not skill_ids:
            raise EvolutionDriverError("baseline workflow has no skill nodes to evolve")
        summary_node = next(
            (node_id for node_id in skill_ids if "summary" in node_id), skill_ids[0],
        )
        if weakness == "latency":
            return (CandidateOperation(
                CandidateOperationKind.SET_CONSTRAINT, summary_node,
                field="max_latency_ms", value=5_000,
            ),)
        if weakness == "risk_rate":
            return (CandidateOperation(
                CandidateOperationKind.SET_CONSTRAINT, skill_ids[-1],
                field="freshness_seconds", value=30,
            ),)
        # Success/evidence/value weaknesses: sharpen the summary title input so
        # downstream consumers get a more specific artefact.
        return (CandidateOperation(
            CandidateOperationKind.SET_INPUT, summary_node,
            field="title",
            value=f"[{weakness}] Market summary",
        ),)


class LlmEvolutionStrategy:
    """Refine the hypothesis and parameters through a structured model.

    The model only shapes text and values; the operation kind, node and
    policy boundary stay under rule control. Any model failure falls back
    to the rule strategy.
    """

    def __init__(
        self, model: StructuredModel, fallback: RuleEvolutionStrategy | None = None,
    ) -> None:
        self._model = model
        self._fallback = fallback or RuleEvolutionStrategy()

    async def plan(
        self, weakness: str, snapshot: Mapping[str, object], baseline: DnaDefinition,
    ) -> tuple[str, tuple[CandidateOperation, ...], str]:
        prompt = {
            "task": "propose_dna_mutation",
            "weakness": weakness,
            "threshold": WEAKNESS_THRESHOLDS[weakness],
            "fitness": dict(snapshot),
            "allowed_operations": ["SET_INPUT", "SET_CONSTRAINT", "SET_CAPABILITY_VERSION"],
            "workflow_nodes": [str(node.get("node_id"))
                               for node in cast("Sequence[Mapping[str, object]]",
                                                baseline.workflow["nodes"])],
        }
        try:
            response = await self._model.generate(prompt)
        except (ValueError, TypeError, RuntimeError, OSError):
            hypothesis = self._fallback.hypothesis(weakness, snapshot)
            operations = self._fallback.operations(weakness, snapshot, baseline)
            return hypothesis, operations, "rule-fallback"
        document = response if isinstance(response, Mapping) else _parse(response)
        if document is None:
            hypothesis = self._fallback.hypothesis(weakness, snapshot)
            operations = self._fallback.operations(weakness, snapshot, baseline)
            return hypothesis, operations, "rule-fallback"
        hypothesis = str(document.get("hypothesis", "")).strip()
        value = document.get("value")
        field_name = str(document.get("field", "title"))
        node_ids = skill_ids(cast(
            "Sequence[Mapping[str, object]]", baseline.workflow["nodes"],
        ))
        node_id = str(document.get("node_id") or next(
            (item for item in node_ids if "summary" in item), node_ids[0],
        ))
        kind = (CandidateOperationKind.SET_CONSTRAINT
                if field_name in {"max_latency_ms", "freshness_seconds"}
                else CandidateOperationKind.SET_INPUT)
        operations = (CandidateOperation(
            kind, node_id, field=field_name,
            value=value if value is not None else f"[{weakness}] Market summary",
        ),)
        if not hypothesis:
            hypothesis = self._fallback.hypothesis(weakness, snapshot)
        return hypothesis, operations, "structured-model"


def skill_ids(nodes: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(node["node_id"]) for node in nodes if str(node.get("type")) == "skill"]


def _parse(raw: str) -> Mapping[str, object] | None:
    import json

    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, Mapping) else None


class EvolutionDriver:
    """Turn a fitness snapshot into a governed evolution plan."""

    def __init__(
        self, database: SQLiteDatabase | None = None, *,
        model: StructuredModel | None = None,
    ) -> None:
        self._database = database
        self._model = model
        self._rule_strategy = RuleEvolutionStrategy()

    async def drive(
        self, baseline: DnaDefinition, *, snapshot: Mapping[str, object],
        new_version: str | None = None,
    ) -> EvolutionPlan:
        if snapshot.get("readiness") == FitnessReadiness.RISK_BLOCKED.value:
            raise EvolutionDriverError("baseline is risk blocked; evolution is paused")
        weakness = detect_weakness(snapshot)
        if weakness is None:
            raise EvolutionDriverError("no fitness weakness above target; nothing to evolve")
        if self._model is not None:
            hypothesis, operations, source = await LlmEvolutionStrategy(
                self._model,
            ).plan(weakness, snapshot, baseline)
        else:
            hypothesis = self._rule_strategy.hypothesis(weakness, snapshot)
            operations = self._rule_strategy.operations(weakness, snapshot, baseline)
            source = "rule"
        parts = str(snapshot["version"]).split(".")
        next_version = new_version or (
            f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3
            and all(part.isdigit() for part in parts) else f"{snapshot['version']}.1"
        )
        return EvolutionPlan(
            weakness=weakness, hypothesis=hypothesis, operations=operations,
            source=source, baseline_version=str(snapshot["version"]),
            new_version=next_version,
            snapshot_revision=int(str(snapshot["revision"])),
        )
