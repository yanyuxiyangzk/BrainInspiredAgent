"""G-01: domain-neutral evolution auto-plan for the generic runtime.

从持久化 ``dna_fitness_snapshot`` 读取任意 ACTIVE 基线的最新快照，经
EvolutionDriver（规则策略，``artifact_label`` 参数化兜底文案）自动检测弱点并
生成受治理操作，再经 ``DnaCandidateGenerator``（候选策略从基线工作流自推导）
落一个候选提案。不绑定任何领域：基线 DNA ID 由调用方指定。治理边界不变——
提案仍是 CANDIDATE，promote 需人工 reason+yes。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from active_agent_platform.foundation import SystemClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.dna import DnaDefinition
from domain_sdk.dna_candidates import (
    CandidateMode,
    CandidatePolicy,
    CandidateRequest,
    DnaCandidateGenerator,
)
from domain_sdk.dna_evolution_driver import EvolutionDriver, detect_weakness
from domain_sdk.experience_dataset import ExperienceDatasetBuilder, ExperienceDatasetError

_SNAPSHOT_KEYS = (
    "success_rate", "evidence_score", "user_value_score", "stability_rate",
    "readiness", "version", "revision",
)


@dataclass(frozen=True, slots=True)
class GenericAutoPlanResult:
    status: str
    weakness: str | None = None
    source: str | None = None
    hypothesis: str | None = None
    proposal_id: str | None = None
    candidate_dna_id: str | None = None
    candidate_version: str | None = None
    baseline_version: str | None = None
    new_version: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "weakness": self.weakness,
            "source": self.source,
            "hypothesis": self.hypothesis,
            "proposal_id": self.proposal_id,
            "candidate_dna_id": self.candidate_dna_id,
            "candidate_version": self.candidate_version,
            "baseline_version": self.baseline_version,
            "new_version": self.new_version,
            "reason": self.reason,
        }


def _baseline_policy(workflow: Mapping[str, object]) -> CandidatePolicy:
    """从基线工作流自推导候选策略：能力/绑定/权限钉死在基线，只放开可变输入。"""
    policy = cast("Mapping[str, object]", workflow["policy"])
    capabilities = frozenset(cast("Sequence[str]", policy["required_capabilities"]))
    nodes = cast("Sequence[Mapping[str, object]]", workflow["nodes"])
    bindings: set[tuple[str, str]] = set()
    permissions: set[str] = set()
    risk = {"PURE": 0, "IDEMPOTENT": 1, "QUERYABLE": 2, "NON_REPLAYABLE": 3}
    max_side_effect = "PURE"
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
        policy_version="generic-evolution/1.0",
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


async def auto_plan_candidate(
    database: SQLiteDatabase, *, proposal_id: str,
    baseline_dna_id: str, dataset_id: str, dataset_version: str,
    baseline_version: str | None = None, artifact_label: str = "artifact",
) -> GenericAutoPlanResult:
    """从持久化 fitness 快照自动生成一个受治理候选提案（领域中性感）。"""
    row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? "
        "AND (version=? OR ? IS NULL) AND status='ACTIVE' "
        "ORDER BY version DESC LIMIT 1",
        (baseline_dna_id, baseline_version, baseline_version),
    )
    if row is None:
        return GenericAutoPlanResult(
            status="REJECTED", reason=f"no ACTIVE baseline DNA: {baseline_dna_id}"
        )
    baseline = DnaDefinition.from_document(
        cast("Mapping[str, object]", json.loads(str(row["document_json"])))
    )
    snapshot_row = await database.fetch_one(
        "SELECT * FROM dna_fitness_snapshot WHERE dna_id=? AND version=? "
        "ORDER BY projected_at DESC LIMIT 1",
        (baseline.dna_id, baseline.version),
    )
    if snapshot_row is None:
        return GenericAutoPlanResult(
            status="REJECTED",
            reason=f"no fitness snapshot for {baseline.dna_id}@{baseline.version}",
        )
    snapshot = {key: snapshot_row[key] for key in _SNAPSHOT_KEYS}
    if str(snapshot["readiness"]) == "RISK_BLOCKED":
        return GenericAutoPlanResult(status="RISK_BLOCKED", baseline_version=baseline.version)
    if detect_weakness(snapshot) is None:
        return GenericAutoPlanResult(status="NO_WEAKNESS", baseline_version=baseline.version)
    plan = await EvolutionDriver(
        database, artifact_label=artifact_label
    ).drive(baseline, snapshot=snapshot)
    try:
        dataset = await ExperienceDatasetBuilder(database, SystemClock()).get(
            dataset_id, dataset_version
        )
    except ExperienceDatasetError as error:
        return GenericAutoPlanResult(
            status="REJECTED", weakness=plan.weakness,
            baseline_version=baseline.version, reason=str(error),
        )
    generator = DnaCandidateGenerator(
        database, SystemClock(), _baseline_policy(baseline.workflow)
    )
    try:
        proposal = await generator.generate(CandidateRequest(
            proposal_id=proposal_id, mode=CandidateMode.MUTATION, base=baseline,
            new_version=plan.new_version, dataset=dataset,
            hypothesis=plan.hypothesis, operations=plan.operations,
            correlation_id=f"generic:auto-plan:{proposal_id}",
        ))
    except ValueError as error:
        return GenericAutoPlanResult(
            status="REJECTED", weakness=plan.weakness,
            baseline_version=baseline.version, reason=str(error),
        )
    return GenericAutoPlanResult(
        status="PROPOSED", weakness=plan.weakness, source=plan.source,
        hypothesis=plan.hypothesis, proposal_id=proposal.proposal_id,
        candidate_dna_id=proposal.candidate.dna_id,
        candidate_version=proposal.candidate.version,
        baseline_version=plan.baseline_version, new_version=plan.new_version,
    )
