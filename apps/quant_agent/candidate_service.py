"""E03 candidate proposal service: operations + hypothesis → governed proposal.

Wraps :class:`DnaCandidateGenerator` with the quant defaults: discovers the
ACTIVE baseline DNA, loads the referenced experience dataset, and derives a
conservative candidate policy from the baseline workflow itself (mutable
inputs/constraints/versions only, capabilities and bindings pinned to the
baseline, side effects capped at the baseline maximum).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from active_agent_platform.foundation import SystemClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.dna import DnaDefinition, DnaStatus
from domain_sdk.dna_candidates import (
    CandidateMode,
    CandidateOperation,
    CandidateOperationKind,
    CandidatePolicy,
    CandidateRequest,
    DnaCandidateGenerator,
)
from domain_sdk.experience_dataset import ExperienceDatasetBuilder

BASELINE_DNA_ID = "workflow.market_summary"
OPERATION_KINDS = tuple(item.value for item in CandidateOperationKind)


class CandidateServiceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProposalResult:
    proposal_id: str
    proposal_digest: str
    base: str
    candidate_dna_id: str
    candidate_version: str
    candidate_content_digest: str
    dataset_manifest_digest: str
    hypothesis: str
    mode: str

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id, "proposal_digest": self.proposal_digest,
            "base": self.base, "candidate_dna_id": self.candidate_dna_id,
            "candidate_version": self.candidate_version,
            "candidate_content_digest": self.candidate_content_digest,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "hypothesis": self.hypothesis, "mode": self.mode,
            "candidate_status": "CANDIDATE",
        }


def parse_operations(documents: Sequence[Mapping[str, object]]) -> tuple[CandidateOperation, ...]:
    """Parse operation documents (as accepted from the CLI) into operations."""
    operations: list[CandidateOperation] = []
    for document in documents:
        kind_value = str(document.get("kind", ""))
        try:
            kind = CandidateOperationKind(kind_value)
        except ValueError as error:
            raise CandidateServiceError(
                f"operation kind must be one of {OPERATION_KINDS}",
            ) from error
        field = document.get("field")
        operations.append(CandidateOperation(
            kind=kind, node_id=str(document.get("node_id", "")),
            field=None if field is None else str(field),
            value=document.get("value"),
        ))
    return tuple(operations)


def bump_version(version: str) -> str:
    """Default next candidate version: patch bump (1.0.0 → 1.0.1)."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise CandidateServiceError(f"base version is not semantic: {version}")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def _baseline_policy(workflow: Mapping[str, object]) -> CandidatePolicy:
    policy = cast("Mapping[str, object]", workflow["policy"])
    capabilities = frozenset(cast("Sequence[str]", policy["required_capabilities"]))
    nodes = cast("Sequence[Mapping[str, object]]", workflow["nodes"])
    bindings: set[tuple[str, str]] = set()
    permissions: set[str] = set()
    max_side_effect = "PURE"
    risk = {"PURE": 0, "IDEMPOTENT": 1, "QUERYABLE": 2, "NON_REPLAYABLE": 3}
    for node in nodes:
        capability = str(node["capability"])
        bindings.add((capability, str(node["capability_version"])))
        constraints = cast("Mapping[str, object]", node["constraints"])
        node_permissions = cast("Sequence[str]", constraints.get("required_permissions", ()))
        permissions.update(node_permissions)
        effect = str(constraints["side_effect"])
        if risk.get(effect, len(risk)) > risk[max_side_effect]:
            max_side_effect = effect
    return CandidatePolicy(
        policy_version="candidate-service/1.0",
        mutable_paths=frozenset({
            "workflow.nodes.*.input.*",
            "workflow.nodes.*.constraints.max_latency_ms",
            "workflow.nodes.*.constraints.freshness_seconds",
            "workflow.nodes.*.capability_version",
        }),
        allowed_capabilities=capabilities,
        allowed_bindings=frozenset(bindings),
        allowed_permissions=frozenset(permissions),
        max_side_effect=max_side_effect,
    )


def _load_baseline(row: Mapping[str, object] | object) -> DnaDefinition:
    document = json.loads(str(row["document_json"]))  # type: ignore[index]
    definition = DnaDefinition.from_document(document)
    if definition.status is not DnaStatus.ACTIVE:
        raise CandidateServiceError("baseline DNA is not ACTIVE")
    return definition


async def propose_candidate(
    database: SQLiteDatabase, *, proposal_id: str,
    operations: Sequence[Mapping[str, object]], hypothesis: str,
    dataset_id: str, dataset_version: str,
    base_dna_id: str = BASELINE_DNA_ID, base_version: str | None = None,
    new_version: str | None = None, mode: str = "MUTATION",
    correlation_id: str | None = None,
) -> ProposalResult:
    """Create a governed candidate proposal from explicit operations."""
    if not hypothesis.strip():
        raise CandidateServiceError("a hypothesis is required")
    row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? "
        "AND (version=? OR ? IS NULL) AND status='ACTIVE' "
        "ORDER BY version DESC LIMIT 1",
        (base_dna_id, base_version, base_version),
    )
    if row is None:
        raise CandidateServiceError(f"no ACTIVE baseline DNA: {base_dna_id}")
    base = _load_baseline(row)
    dataset = await ExperienceDatasetBuilder(database, SystemClock()).get(
        dataset_id, dataset_version,
    )
    try:
        mode_value = CandidateMode(mode)
    except ValueError as error:
        raise CandidateServiceError("mode must be MUTATION or CROSSOVER") from error
    generator = DnaCandidateGenerator(
        database, SystemClock(), _baseline_policy(base.workflow),
    )
    try:
        proposal = await generator.generate(CandidateRequest(
            proposal_id=proposal_id, mode=mode_value, base=base,
            new_version=new_version or bump_version(base.version), dataset=dataset,
            hypothesis=hypothesis, operations=parse_operations(operations),
            correlation_id=correlation_id or f"cli:evolution:propose:{proposal_id}",
        ))
    except ValueError as error:
        raise CandidateServiceError(str(error)) from error
    return ProposalResult(
        proposal_id=proposal.proposal_id, proposal_digest=proposal.proposal_digest,
        base=base.content_digest, candidate_dna_id=proposal.candidate.dna_id,
        candidate_version=proposal.candidate.version,
        candidate_content_digest=proposal.candidate.content_digest,
        dataset_manifest_digest=proposal.dataset_manifest_digest,
        hypothesis=proposal.hypothesis, mode=proposal.mode.value,
    )
