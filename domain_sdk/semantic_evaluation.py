"""X-04: semantic-memory evaluation set and error-recall metrics.

从已验证语义记忆确定性构建评估集，用可注入的检索器（内置确定性
``DirectMatchRetriever``；向量库按路线图延后到 X-04 结论之后）度量召回率
与"错误召回"——命中被矛盾/拒绝/过期记忆即计错。产出机器可读的离线评估
报告（PASSED/FAILED + 指标 + 逐案例结果 + 防篡改 digest round-trip）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from active_agent_platform.semantic_memory import SemanticMemoryRecord

_DOCUMENT_FORMAT = 1
_EXCLUDED_KEYS = {"content_digest"}


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一条检索评估：查询 + 必须命中的记忆 + 禁止命中的记忆。"""

    query: Mapping[str, object]
    expected_memory_ids: frozenset[str]
    forbidden_memory_ids: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", dict(self.query))
        if not self.expected_memory_ids:
            raise ValueError("evaluation case requires at least one expected memory")
        if self.expected_memory_ids & self.forbidden_memory_ids:
            raise ValueError("expected and forbidden memory sets must not overlap")

    def to_document(self) -> dict[str, object]:
        return {
            "query": dict(self.query),
            "expected_memory_ids": sorted(self.expected_memory_ids),
            "forbidden_memory_ids": sorted(self.forbidden_memory_ids),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> EvaluationCase:
        return cls(
            dict(cast("Mapping[str, object]", document["query"])),
            frozenset(cast("Sequence[str]", document["expected_memory_ids"])),
            frozenset(cast("Sequence[str]", document.get("forbidden_memory_ids", ()))),
        )


@dataclass(frozen=True, slots=True)
class SemanticEvaluationSet:
    """版本化评估集；从真实语料构建，文档携带防篡改 digest。"""

    cases: tuple[EvaluationCase, ...]

    @classmethod
    def build_from_corpus(
        cls,
        validated: Sequence[SemanticMemoryRecord],
        *,
        forbidden_ids: frozenset[str],
    ) -> SemanticEvaluationSet:
        cases = tuple(
            EvaluationCase(
                {
                    "claim_key": record.candidate.claim_key,
                    "scope": dict(record.candidate.scope),
                    "claim_value": record.candidate.claim_value,
                },
                frozenset({record.memory_id}),
                forbidden_ids,
            )
            for record in validated
        )
        return cls(cases)

    def to_document(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": _DOCUMENT_FORMAT,
            "cases": [case.to_document() for case in self.cases],
        }
        payload["content_digest"] = _digest(
            {key: value for key, value in payload.items() if key not in _EXCLUDED_KEYS}
        )
        return payload

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> SemanticEvaluationSet:
        if document.get("format") != _DOCUMENT_FORMAT:
            raise ValueError("unsupported evaluation set format")
        stored = document.get("content_digest")
        expected = _digest(
            {key: value for key, value in document.items() if key not in _EXCLUDED_KEYS}
        )
        if stored != expected:
            raise ValueError("evaluation set content digest mismatch")
        return cls(tuple(
            EvaluationCase.from_document(cast("Mapping[str, object]", case))
            for case in cast("Sequence[object]", document["cases"])
        ))


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    """离线评估门槛：召回下界、错误召回上界与最小案例数。"""

    min_recall_rate: float = 0.9
    max_error_recall_rate: float = 0.0
    min_cases: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_recall_rate <= 1.0:
            raise ValueError("recall rate bound must stay within [0, 1]")
        if not 0.0 <= self.max_error_recall_rate <= 1.0:
            raise ValueError("error recall bound must stay within [0, 1]")
        if self.min_cases < 1:
            raise ValueError("min_cases must be positive")


class Retriever(Protocol):
    """检索接缝：查询 → 命中的记忆 ID；内置确定性实现，向量库可替换。"""

    async def recall(self, query: Mapping[str, object]) -> tuple[str, ...]: ...


class DirectMatchRetriever:
    """确定性直配检索：claim_key 恒等、scope 全等；query 携带 claim_value 时一并约束。"""

    def __init__(self, corpus: Sequence[SemanticMemoryRecord]) -> None:
        self._corpus = tuple(corpus)

    async def recall(self, query: Mapping[str, object]) -> tuple[str, ...]:
        query_value = query.get("claim_value")
        return tuple(
            record.memory_id
            for record in self._corpus
            if record.candidate.claim_key == query.get("claim_key")
            and (query_value is None or record.candidate.claim_value == query_value)
            and dict(record.candidate.scope) == query.get("scope")
        )


@dataclass(frozen=True, slots=True)
class OfflineEvaluationReport:
    correlation_id: str
    status: str
    policy_digest: str
    metrics: Mapping[str, float]
    case_outcomes: tuple[Mapping[str, object], ...]
    failure_reasons: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": _DOCUMENT_FORMAT,
            "correlation_id": self.correlation_id,
            "status": self.status,
            "policy_digest": self.policy_digest,
            "metrics": dict(self.metrics),
            "case_outcomes": [dict(case) for case in self.case_outcomes],
            "failure_reasons": list(self.failure_reasons),
        }
        payload["content_digest"] = _digest(
            {key: value for key, value in payload.items() if key not in _EXCLUDED_KEYS}
        )
        return payload

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> OfflineEvaluationReport:
        if document.get("format") != _DOCUMENT_FORMAT:
            raise ValueError("unsupported evaluation report format")
        stored = document.get("content_digest")
        expected = _digest(
            {key: value for key, value in document.items() if key not in _EXCLUDED_KEYS}
        )
        if stored != expected:
            raise ValueError("evaluation report content digest mismatch")
        return cls(
            correlation_id=str(document["correlation_id"]),
            status=str(document["status"]),
            policy_digest=str(document["policy_digest"]),
            metrics={
                str(key): float(cast("float", value))
                for key, value in cast("Mapping[str, object]", document["metrics"]).items()
            },
            case_outcomes=tuple(
                cast("Mapping[str, object]", case)
                for case in cast("Sequence[object]", document["case_outcomes"])
            ),
            failure_reasons=tuple(
                str(reason) for reason in cast("Sequence[object]", document["failure_reasons"])
            ),
        )


async def run_offline_evaluation(
    evaluation_set: SemanticEvaluationSet,
    retriever: Retriever,
    policy: EvaluationPolicy,
    *,
    correlation_id: str,
) -> OfflineEvaluationReport:
    """逐案例检索并计算召回/错误召回指标；不写库、无时钟依赖。"""
    total_expected = 0
    recalled_expected = 0
    error_cases = 0
    passed_cases = 0
    outcomes: list[dict[str, object]] = []
    for case in evaluation_set.cases:
        hits = frozenset(await retriever.recall(case.query))
        expected_hits = len(case.expected_memory_ids & hits)
        forbidden_hits = sorted(case.forbidden_memory_ids & hits)
        total_expected += len(case.expected_memory_ids)
        recalled_expected += expected_hits
        case_passed = expected_hits == len(case.expected_memory_ids) and not forbidden_hits
        passed_cases += int(case_passed)
        error_cases += int(bool(forbidden_hits))
        outcomes.append({
            "query": dict(case.query),
            "recalled": sorted(hits),
            "expected_hits": expected_hits,
            "forbidden_hits": forbidden_hits,
            "passed": case_passed,
        })
    case_count = len(evaluation_set.cases)
    metrics: dict[str, float] = {
        "cases": float(case_count),
        "case_pass_rate": passed_cases / case_count if case_count else 0.0,
        "recall_rate": recalled_expected / total_expected if total_expected else 0.0,
        "error_recall_rate": error_cases / case_count if case_count else 0.0,
    }
    reasons: list[str] = []
    if case_count < policy.min_cases:
        reasons.append(f"cases {case_count} below minimum {policy.min_cases}")
    if metrics["recall_rate"] < policy.min_recall_rate:
        reasons.append(
            f"recall_rate {metrics['recall_rate']:.3f} below floor {policy.min_recall_rate:.3f}"
        )
    if metrics["error_recall_rate"] > policy.max_error_recall_rate:
        reasons.append(
            f"error_recall_rate {metrics['error_recall_rate']:.3f} exceeds budget "
            f"{policy.max_error_recall_rate:.3f}"
        )
    return OfflineEvaluationReport(
        correlation_id=correlation_id,
        status="PASSED" if not reasons else "FAILED",
        policy_digest=_digest({
            "min_recall_rate": policy.min_recall_rate,
            "max_error_recall_rate": policy.max_error_recall_rate,
            "min_cases": policy.min_cases,
        }),
        metrics=metrics,
        case_outcomes=tuple(outcomes),
        failure_reasons=tuple(reasons),
    )
