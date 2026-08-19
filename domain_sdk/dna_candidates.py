"""Governed mutation and crossover proposals that can only produce Candidate DNA."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from active_agent_platform.workflow import WorkflowValidator
from brain_kernel.ports import Clock
from domain_sdk.dna import DnaDefinition, DnaParent, DnaStatus
from domain_sdk.experience_dataset import ExperienceDataset


class DnaCandidateError(ValueError):
    pass


class CandidateMode(StrEnum):
    MUTATION = "MUTATION"
    CROSSOVER = "CROSSOVER"


class CandidateOperationKind(StrEnum):
    SET_INPUT = "SET_INPUT"
    SET_CONSTRAINT = "SET_CONSTRAINT"
    SET_CAPABILITY_VERSION = "SET_CAPABILITY_VERSION"
    ADD_SKILL_NODE = "ADD_SKILL_NODE"
    REMOVE_NODE = "REMOVE_NODE"
    REPLACE_FROM_DONOR = "REPLACE_FROM_DONOR"


@dataclass(frozen=True, slots=True)
class CandidateOperation:
    kind: CandidateOperationKind
    node_id: str
    field: str | None = None
    value: object = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.node_id) is None:
            raise DnaCandidateError("candidate operation node_id is invalid")

    def to_document(self) -> dict[str, object]:
        return {"kind": self.kind.value, "node_id": self.node_id,
                "field": self.field, "value": self.value}


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    policy_version: str
    mutable_paths: frozenset[str]
    allowed_capabilities: frozenset[str]
    allowed_bindings: frozenset[tuple[str, str]]
    allowed_permissions: frozenset[str] = frozenset()
    max_side_effect: str = "QUERYABLE"
    max_operations: int = 8
    max_nodes: int = 100

    def __post_init__(self) -> None:
        if not self.policy_version or not self.mutable_paths:
            raise DnaCandidateError("candidate policy and mutable paths must not be empty")
        if self.max_side_effect not in _SIDE_EFFECT_RISK:
            raise DnaCandidateError("candidate max_side_effect is invalid")
        if self.max_operations < 1 or self.max_nodes < 1:
            raise DnaCandidateError("candidate limits must be positive")
        if any(path.startswith(_IMMUTABLE_PREFIXES) for path in self.mutable_paths):
            raise DnaCandidateError("mutable paths include an immutable boundary")


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    proposal_id: str
    mode: CandidateMode
    base: DnaDefinition
    new_version: str
    dataset: ExperienceDataset
    hypothesis: str
    operations: tuple[CandidateOperation, ...]
    correlation_id: str
    donor: DnaDefinition | None = None

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[a-zA-Z0-9_.:-]{3,128}", self.proposal_id) is None
                or not self.hypothesis.strip() or not self.correlation_id):
            raise DnaCandidateError("candidate proposal metadata is invalid")
        if not self.operations:
            raise DnaCandidateError("candidate proposal must contain operations")
        if self.mode is CandidateMode.MUTATION and self.donor is not None:
            raise DnaCandidateError("mutation cannot declare a donor")
        if self.mode is CandidateMode.CROSSOVER and self.donor is None:
            raise DnaCandidateError("crossover requires a donor")
        has_crossover = any(item.kind is CandidateOperationKind.REPLACE_FROM_DONOR
                            for item in self.operations)
        if self.mode is CandidateMode.CROSSOVER and not has_crossover:
            raise DnaCandidateError("crossover requires a donor replacement operation")
        if self.mode is CandidateMode.MUTATION and has_crossover:
            raise DnaCandidateError("mutation cannot use a donor replacement operation")


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    proposal_id: str
    mode: CandidateMode
    candidate: DnaDefinition
    proposal_digest: str
    dataset_manifest_digest: str
    policy_version: str
    hypothesis: str
    operations: tuple[CandidateOperation, ...]
    created_at: datetime


_ELIGIBLE_PARENT = frozenset({DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY,
                              DnaStatus.ACTIVE, DnaStatus.DEPRECATED})
_SIDE_EFFECT_RISK = {"PURE": 0, "IDEMPOTENT": 1, "QUERYABLE": 2, "NON_REPLAYABLE": 3}
_SKILL_FIELDS = frozenset({
    "node_id", "type", "depends_on", "timeout_seconds", "retry", "capability",
    "capability_version", "input", "constraints", "output_schema_ref", "idempotency_key",
})
_CONSTRAINT_FIELDS = frozenset({
    "side_effect", "max_latency_ms", "freshness_seconds", "required_permissions",
})
_IMMUTABLE_PREFIXES = (
    "dna_", "workflow.spec_version", "workflow.workflow_id", "workflow.input_schema",
    "workflow.output_mapping", "workflow.policy", "workflow.nodes.*.constraints.side_effect",
    "workflow.nodes.*.constraints.required_permissions",
)


class DnaCandidateGenerator:
    def __init__(self, database: SQLiteDatabase, clock: Clock, policy: CandidatePolicy) -> None:
        self._database = database
        self._clock = clock
        self._policy = policy

    async def generate(self, request: CandidateRequest) -> CandidateProposal:
        if len(request.operations) > self._policy.max_operations:
            raise DnaCandidateError("candidate exceeds operation limit")
        _parent(request.base, "base")
        if request.donor is not None:
            _parent(request.donor, "donor")
        if request.new_version in {request.base.version,
                                   None if request.donor is None else request.donor.version}:
            raise DnaCandidateError("candidate version must be new")
        workflow = _plain_workflow(request.base)
        donor_nodes = {} if request.donor is None else _node_map(_plain_workflow(request.donor))
        changes: list[dict[str, object]] = []
        for operation in request.operations:
            path = _operation_path(operation)
            if not any(fnmatch.fnmatchcase(path, allowed) for allowed in self._policy.mutable_paths):
                raise DnaCandidateError(f"candidate operation is outside mutable_paths: {path}")
            _apply(workflow, donor_nodes, operation)
            changes.append(operation.to_document() | {"path": path})
        workflow["version"] = request.new_version
        self._validate_workflow(workflow, request.base)
        parents = [request.base]
        if request.donor is not None:
            parents.append(request.donor)
        candidate = DnaDefinition.from_workflow(
            workflow, dna_id=request.base.dna_id, version=request.new_version,
            parent_dna=tuple(DnaParent(item.dna_id, item.version, item.content_digest)
                             for item in parents),
            generator={"name": "dna-candidate-generator", "version": "1.0",
                       "mode": request.mode.value, "proposal_id": request.proposal_id,
                       "dataset_manifest": request.dataset.manifest.manifest_digest,
                       "policy_version": self._policy.policy_version},
        )
        proposal_document = {
            "proposal_id": request.proposal_id, "mode": request.mode.value,
            "candidate": candidate.to_document(), "base_digest": request.base.content_digest,
            "donor_digest": None if request.donor is None else request.donor.content_digest,
            "dataset_manifest_digest": request.dataset.manifest.manifest_digest,
            "policy_version": self._policy.policy_version,
            "hypothesis": request.hypothesis, "operations": changes,
        }
        proposal_digest = _digest(proposal_document)
        now = _utc(self._clock.now())
        async with self._database.transaction() as transaction:
            await self._validate_sources(transaction, request)
            existing = await transaction.fetch_one(
                "SELECT * FROM dna_candidate_proposal WHERE proposal_id=?",
                (request.proposal_id,),
            )
            if existing is not None:
                if str(existing["proposal_digest"]) != proposal_digest:
                    raise DnaCandidateError("proposal ID already exists with different content")
                return _proposal(existing)
            duplicate = await transaction.fetch_one(
                """SELECT proposal_id FROM dna_candidate_proposal
                   WHERE candidate_dna_id=? AND candidate_content_digest=?""",
                (candidate.dna_id, candidate.content_digest),
            )
            if duplicate is not None:
                raise DnaCandidateError("candidate content already has a proposal")
            await transaction.execute(
                "INSERT INTO dna_candidate_proposal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.proposal_id, request.mode.value, candidate.dna_id, candidate.version,
                 candidate.content_digest, request.base.dna_id, request.base.version,
                 request.base.content_digest,
                 None if request.donor is None else request.donor.dna_id,
                 None if request.donor is None else request.donor.version,
                 None if request.donor is None else request.donor.content_digest,
                 request.dataset.manifest.dataset_id, request.dataset.manifest.version,
                 request.dataset.manifest.manifest_digest, self._policy.policy_version,
                 request.hypothesis, _json(changes), _json(candidate.to_document()),
                 proposal_digest, _time(now), request.correlation_id),
            )
        return CandidateProposal(
            request.proposal_id, request.mode, candidate, proposal_digest,
            request.dataset.manifest.manifest_digest, self._policy.policy_version,
            request.hypothesis, request.operations, now,
        )

    async def get(self, proposal_id: str) -> CandidateProposal:
        row = await self._database.fetch_one(
            "SELECT * FROM dna_candidate_proposal WHERE proposal_id=?", (proposal_id,)
        )
        if row is None:
            raise DnaCandidateError(f"candidate proposal not found: {proposal_id}")
        return _proposal(row)

    def _validate_workflow(
        self, workflow: dict[str, object], base: DnaDefinition,
    ) -> None:
        if any(workflow[key] != _plain_workflow(base)[key]
               for key in ("spec_version", "workflow_id", "input_schema", "output_mapping",
                           "policy")):
            raise DnaCandidateError("candidate changed an immutable workflow boundary")
        nodes = cast(list[Mapping[str, object]], workflow["nodes"])
        if len(nodes) > self._policy.max_nodes:
            raise DnaCandidateError("candidate exceeds node limit")
        declared = set(cast(Sequence[str], cast(Mapping[str, object], workflow["policy"])
                            ["required_capabilities"]))
        for node in nodes:
            if node["type"] != "skill":
                continue
            capability = str(node["capability"])
            if capability not in declared or capability not in self._policy.allowed_capabilities:
                raise DnaCandidateError("candidate requests a denied capability")
            binding = (capability, str(node["capability_version"]))
            if binding not in self._policy.allowed_bindings:
                raise DnaCandidateError("candidate requests a denied capability binding")
            constraints = cast(Mapping[str, object], node["constraints"])
            if not set(node) <= _SKILL_FIELDS or not set(constraints) <= _CONSTRAINT_FIELDS:
                raise DnaCandidateError("candidate skill node contains unsupported fields")
            latency, freshness = constraints.get("max_latency_ms"), constraints.get("freshness_seconds")
            if (latency is not None and (not isinstance(latency, int) or isinstance(latency, bool)
                                         or latency < 1)
                    or freshness is not None
                    and (not isinstance(freshness, int) or isinstance(freshness, bool)
                         or freshness < 0)):
                raise DnaCandidateError("candidate skill constraint is invalid")
            raw_permissions = constraints.get("required_permissions", ())
            if (not isinstance(raw_permissions, Sequence)
                    or isinstance(raw_permissions, str | bytes)
                    or any(not isinstance(item, str) for item in raw_permissions)):
                raise DnaCandidateError("candidate skill permissions are invalid")
            permissions = set(cast(Sequence[str], raw_permissions))
            if not permissions <= self._policy.allowed_permissions:
                raise DnaCandidateError("candidate requests a denied permission")
            side_effect = str(constraints["side_effect"])
            if side_effect not in _SIDE_EFFECT_RISK:
                raise DnaCandidateError("candidate side-effect is invalid")
            if _SIDE_EFFECT_RISK[side_effect] > _SIDE_EFFECT_RISK[self._policy.max_side_effect]:
                raise DnaCandidateError("candidate exceeds side-effect boundary")
        try:
            WorkflowValidator(max_nodes=self._policy.max_nodes).validate(workflow)
        except ValueError as error:
            raise DnaCandidateError("candidate workflow validation failed") from error

    async def _validate_sources(
        self, transaction: SQLiteTransaction, request: CandidateRequest,
    ) -> None:
        dataset = await transaction.fetch_one(
            """SELECT manifest_digest FROM dna_experience_dataset
               WHERE dataset_id=? AND version=?""",
            (request.dataset.manifest.dataset_id, request.dataset.manifest.version),
        )
        if dataset is None or str(dataset["manifest_digest"]) != request.dataset.manifest.manifest_digest:
            raise DnaCandidateError("candidate dataset is missing or manifest does not match")
        for parent in (request.base, request.donor):
            if parent is None:
                continue
            row = await transaction.fetch_one(
                """SELECT content_digest FROM dna_definition
                   WHERE dna_id=? AND version=?""", (parent.dna_id, parent.version),
            )
            if row is None or str(row["content_digest"]) != parent.content_digest:
                raise DnaCandidateError("candidate parent is missing or digest does not match")
            sample = await transaction.fetch_one(
                """SELECT sample_id FROM dna_experience_sample
                   WHERE dataset_id=? AND dataset_version=? AND content_digest=? LIMIT 1""",
                (request.dataset.manifest.dataset_id, request.dataset.manifest.version,
                 parent.content_digest),
            )
            if sample is None:
                raise DnaCandidateError("candidate parent is absent from the source dataset")


def _operation_path(operation: CandidateOperation) -> str:
    if operation.kind is CandidateOperationKind.SET_INPUT:
        if not operation.field:
            raise DnaCandidateError("SET_INPUT requires a field")
        return f"workflow.nodes.{operation.node_id}.input.{operation.field}"
    if operation.kind is CandidateOperationKind.SET_CONSTRAINT:
        if operation.field not in {"max_latency_ms", "freshness_seconds"}:
            raise DnaCandidateError("constraint is not mutable")
        return f"workflow.nodes.{operation.node_id}.constraints.{operation.field}"
    if operation.kind is CandidateOperationKind.SET_CAPABILITY_VERSION:
        return f"workflow.nodes.{operation.node_id}.capability_version"
    if operation.kind in {CandidateOperationKind.ADD_SKILL_NODE,
                          CandidateOperationKind.REMOVE_NODE}:
        return "workflow.nodes"
    return f"workflow.nodes.{operation.node_id}"


def _apply(
    workflow: dict[str, object], donor_nodes: Mapping[str, dict[str, object]],
    operation: CandidateOperation,
) -> None:
    nodes = cast(list[dict[str, object]], workflow["nodes"])
    by_id = {str(node["node_id"]): node for node in nodes}
    if operation.kind is CandidateOperationKind.ADD_SKILL_NODE:
        if operation.node_id in by_id or not isinstance(operation.value, Mapping):
            raise DnaCandidateError("added node is invalid or duplicated")
        added = cast(dict[str, object], _plain(operation.value))
        if added.get("node_id") != operation.node_id or added.get("type") != "skill":
            raise DnaCandidateError("only a complete skill node can be added")
        nodes.append(added)
        return
    node = by_id.get(operation.node_id)
    if node is None:
        raise DnaCandidateError(f"candidate node not found: {operation.node_id}")
    if operation.kind is CandidateOperationKind.REMOVE_NODE:
        nodes.remove(node)
    elif operation.kind is CandidateOperationKind.REPLACE_FROM_DONOR:
        donor = donor_nodes.get(operation.node_id)
        if donor is None:
            raise DnaCandidateError("crossover donor node not found")
        nodes[nodes.index(node)] = cast(dict[str, object], _plain(donor))
    elif operation.kind is CandidateOperationKind.SET_INPUT:
        if node.get("type") != "skill" or not operation.field:
            raise DnaCandidateError("SET_INPUT requires a skill node and field")
        cast(dict[str, object], node["input"])[operation.field] = _plain(operation.value)
    elif operation.kind is CandidateOperationKind.SET_CONSTRAINT:
        if node.get("type") != "skill" or operation.field is None:
            raise DnaCandidateError("SET_CONSTRAINT requires a skill node")
        cast(dict[str, object], node["constraints"])[operation.field] = _plain(operation.value)
    else:
        if node.get("type") != "skill" or not isinstance(operation.value, str):
            raise DnaCandidateError("capability version mutation is invalid")
        node["capability_version"] = operation.value


def _parent(dna: DnaDefinition, label: str) -> None:
    if dna.status not in _ELIGIBLE_PARENT:
        raise DnaCandidateError(f"{label} DNA has not passed validation")


def _plain_workflow(dna: DnaDefinition) -> dict[str, object]:
    return cast(dict[str, object], _plain(dna.workflow))


def _node_map(workflow: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {str(node["node_id"]): node
            for node in cast(list[dict[str, object]], workflow["nodes"])}


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _proposal(row: sqlite3.Row) -> CandidateProposal:
    operations = tuple(CandidateOperation(
        CandidateOperationKind(str(item["kind"])), str(item["node_id"]),
        None if item["field"] is None else str(item["field"]), item["value"],
    ) for item in json.loads(str(row["operations_json"])))
    return CandidateProposal(
        str(row["proposal_id"]), CandidateMode(str(row["mode"])),
        DnaDefinition.from_document(json.loads(str(row["candidate_document_json"]))),
        str(row["proposal_digest"]), str(row["dataset_manifest_digest"]),
        str(row["policy_version"]), str(row["hypothesis"]), operations,
        datetime.fromisoformat(str(row["created_at"])).astimezone(UTC),
    )


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaCandidateError("candidate time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")
