"""X-06: memory-enhanced decision A/B loop (dual-track comparison).

同一决策案例集分两臂执行：无记忆基线臂与记忆增强臂（可注入任何决策器，
增强臂的决策须报告其检索命中的记忆 ID）。确定性裁判对两臂决策打分，报告
质量增量与"错误召回"（增强臂检索到禁用/被矛盾记忆）。通过条件：质量增量
≥ 下界、错误召回 ≤ 预算、案例数达标——记忆只有在真实提升决策且不引入
污染时才算赢。纯确定性库：不写库、无时钟依赖。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

_DOCUMENT_FORMAT = 1
_EXCLUDED_KEYS = {"content_digest"}


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionCase:
    case_id: str
    context: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("decision case requires a case_id")
        object.__setattr__(self, "context", dict(self.context))


@dataclass(frozen=True, slots=True)
class Decision:
    """一次决策：决策文档 + 该臂检索命中的记忆 ID（基线臂为空）。"""

    document: Mapping[str, object]
    recalled_memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", dict(self.document))


class Decider(Protocol):
    async def decide(self, case: DecisionCase) -> Decision: ...


@dataclass(frozen=True, slots=True)
class DecisionQuality:
    successful: bool
    quality: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("decision quality must stay within [0, 1]")


class Judge(Protocol):
    def assess(self, case: DecisionCase, decision: Decision) -> DecisionQuality: ...


@dataclass(frozen=True, slots=True)
class AbPolicy:
    min_quality_delta: float = 0.1
    max_error_recall_rate: float = 0.0
    min_cases: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_quality_delta <= 1.0:
            raise ValueError("quality delta floor must stay within [0, 1]; memory must not degrade decisions")
        if not 0.0 <= self.max_error_recall_rate <= 1.0:
            raise ValueError("error recall budget must stay within [0, 1]")
        if self.min_cases < 1:
            raise ValueError("min_cases must be positive")


@dataclass(frozen=True, slots=True)
class AbReport:
    correlation_id: str
    status: str
    policy_digest: str
    metrics: Mapping[str, float]
    case_rows: tuple[Mapping[str, object], ...]
    failure_reasons: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": _DOCUMENT_FORMAT,
            "correlation_id": self.correlation_id,
            "status": self.status,
            "policy_digest": self.policy_digest,
            "metrics": dict(self.metrics),
            "case_rows": [dict(row) for row in self.case_rows],
            "failure_reasons": list(self.failure_reasons),
        }
        payload["content_digest"] = _digest(
            {key: value for key, value in payload.items() if key not in _EXCLUDED_KEYS}
        )
        return payload

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> AbReport:
        if document.get("format") != _DOCUMENT_FORMAT:
            raise ValueError("unsupported A/B report format")
        stored = document.get("content_digest")
        expected = _digest(
            {key: value for key, value in document.items() if key not in _EXCLUDED_KEYS}
        )
        if stored != expected:
            raise ValueError("A/B report content digest mismatch")
        return cls(
            correlation_id=str(document["correlation_id"]),
            status=str(document["status"]),
            policy_digest=str(document["policy_digest"]),
            metrics={
                str(key): float(cast("float", value))
                for key, value in cast("Mapping[str, object]", document["metrics"]).items()
            },
            case_rows=tuple(
                cast("Mapping[str, object]", row)
                for row in cast("Sequence[object]", document["case_rows"])
            ),
            failure_reasons=tuple(
                str(reason) for reason in cast("Sequence[object]", document["failure_reasons"])
            ),
        )


async def run_ab_comparison(
    cases: Sequence[DecisionCase],
    baseline: Decider,
    memory_arm: Decider,
    judge: Judge,
    *,
    forbidden_memory_ids: frozenset[str],
    policy: AbPolicy,
    correlation_id: str,
) -> AbReport:
    """双轨执行同案例集并裁决；质量增量与错误召回同时达标才 PASSED。"""
    baseline_total = 0.0
    memory_total = 0.0
    baseline_successes = 0
    memory_successes = 0
    error_cases = 0
    rows: list[dict[str, object]] = []
    for case in cases:
        baseline_decision = await baseline.decide(case)
        memory_decision = await memory_arm.decide(case)
        baseline_quality = judge.assess(case, baseline_decision)
        memory_quality = judge.assess(case, memory_decision)
        recalled = frozenset(memory_decision.recalled_memory_ids)
        forbidden_hits = sorted(recalled & forbidden_memory_ids)
        baseline_total += baseline_quality.quality
        memory_total += memory_quality.quality
        baseline_successes += int(baseline_quality.successful)
        memory_successes += int(memory_quality.successful)
        error_cases += int(bool(forbidden_hits))
        rows.append({
            "case_id": case.case_id,
            "baseline_successful": baseline_quality.successful,
            "memory_successful": memory_quality.successful,
            "baseline_quality": baseline_quality.quality,
            "memory_quality": memory_quality.quality,
            "recalled_memory_ids": sorted(recalled),
            "forbidden_hits": forbidden_hits,
        })
    case_count = len(cases)
    baseline_mean = baseline_total / case_count if case_count else 0.0
    memory_mean = memory_total / case_count if case_count else 0.0
    metrics: dict[str, float] = {
        "cases": float(case_count),
        "baseline_quality": baseline_mean,
        "memory_quality": memory_mean,
        "quality_delta": memory_mean - baseline_mean,
        "baseline_successes": float(baseline_successes),
        "memory_successes": float(memory_successes),
        "error_recall_rate": error_cases / case_count if case_count else 0.0,
    }
    reasons: list[str] = []
    if case_count < policy.min_cases:
        reasons.append(f"cases {case_count} below minimum {policy.min_cases}")
    if metrics["quality_delta"] < policy.min_quality_delta:
        reasons.append(
            f"quality_delta {metrics['quality_delta']:.3f} below floor "
            f"{policy.min_quality_delta:.3f}"
        )
    if metrics["error_recall_rate"] > policy.max_error_recall_rate:
        reasons.append(
            f"error_recall_rate {metrics['error_recall_rate']:.3f} exceeds budget "
            f"{policy.max_error_recall_rate:.3f}"
        )
    return AbReport(
        correlation_id=correlation_id,
        status="PASSED" if not reasons else "FAILED",
        policy_digest=_digest({
            "min_quality_delta": policy.min_quality_delta,
            "max_error_recall_rate": policy.max_error_recall_rate,
            "min_cases": policy.min_cases,
        }),
        metrics=metrics,
        case_rows=tuple(rows),
        failure_reasons=tuple(reasons),
    )
