"""E05 application wiring: fitness weakness → governed candidate proposal.

``/evolution auto-plan`` 把 EvolutionDriver（弱点检测 + 受治理操作生成）接进
应用层：读取与 CLI 查询同一证据源（``dna_fitness_snapshot``），自动产出弱点、
假设与操作，并复用 E03 受治理提案服务落库——全程无需人工提供
operations/hypothesis。治理门（promote 需人工 reason+yes）保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass

from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.candidate_service import (
    BASELINE_DNA_ID,
    CandidateServiceError,
    _load_baseline,
    propose_candidate,
)
from domain_sdk.dna_evolution_driver import (
    WEAKNESS_THRESHOLDS,
    EvolutionDriver,
    detect_weakness,
)

SNAPSHOT_KEYS = (
    "success_rate", "evidence_score", "user_value_score", "stability_rate",
    "readiness", "version", "revision",
)


@dataclass(frozen=True, slots=True)
class AutoPlanResult:
    """auto-plan 的裁决与产物；PROPOSED 时携带提案身份。"""

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


async def auto_plan_candidate(
    database: SQLiteDatabase, *, proposal_id: str,
    dataset_id: str, dataset_version: str,
    baseline_dna_id: str = BASELINE_DNA_ID, baseline_version: str | None = None,
) -> AutoPlanResult:
    """从持久化 fitness 快照自动生成一个受治理候选提案。"""
    base_row = await database.fetch_one(
        "SELECT document_json FROM dna_definition WHERE dna_id=? "
        "AND (version=? OR ? IS NULL) AND status='ACTIVE' "
        "ORDER BY version DESC LIMIT 1",
        (baseline_dna_id, baseline_version, baseline_version),
    )
    if base_row is None:
        return AutoPlanResult(
            status="REJECTED", reason=f"no ACTIVE baseline DNA: {baseline_dna_id}"
        )
    baseline = _load_baseline(base_row)
    snapshot_row = await database.fetch_one(
        "SELECT * FROM dna_fitness_snapshot WHERE dna_id=? AND version=? "
        "ORDER BY projected_at DESC LIMIT 1",
        (baseline.dna_id, baseline.version),
    )
    if snapshot_row is None:
        return AutoPlanResult(
            status="REJECTED",
            reason=f"no fitness snapshot for {baseline.dna_id}@{baseline.version}",
        )
    snapshot = {key: snapshot_row[key] for key in SNAPSHOT_KEYS}
    if str(snapshot["readiness"]) == "RISK_BLOCKED":
        return AutoPlanResult(status="RISK_BLOCKED", baseline_version=baseline.version)
    if detect_weakness(snapshot) is None:
        return AutoPlanResult(status="NO_WEAKNESS", baseline_version=baseline.version)
    plan = await EvolutionDriver(database).drive(baseline, snapshot=snapshot)
    try:
        proposal = await propose_candidate(
            database, proposal_id=proposal_id,
            operations=plan.operations_document(), hypothesis=plan.hypothesis,
            dataset_id=dataset_id, dataset_version=dataset_version,
            new_version=plan.new_version,
        )
    except CandidateServiceError as error:
        return AutoPlanResult(
            status="REJECTED", weakness=plan.weakness,
            baseline_version=baseline.version, reason=str(error),
        )
    return AutoPlanResult(
        status="PROPOSED", weakness=plan.weakness, source=plan.source,
        hypothesis=plan.hypothesis, proposal_id=proposal.proposal_id,
        candidate_dna_id=proposal.candidate_dna_id,
        candidate_version=proposal.candidate_version,
        baseline_version=plan.baseline_version, new_version=plan.new_version,
    )


__all__ = ["WEAKNESS_THRESHOLDS", "AutoPlanResult", "auto_plan_candidate"]
