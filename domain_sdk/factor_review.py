"""Deterministic hard-gate review for generated factor candidates (L-005).

架构 §3.2：未知字段/算子、跨量纲、非法窗口和数据越界直接拒绝；表达式冗余
恒等式、复杂度和树深度在发布前过滤。审查是纯确定性规则库——不调用模型、
不回测、不写库；LLM 审查（L-006 的独立 SkillBinding Sub-agent）只能叠加在
本硬门槛之上，不能绕过它。验收契约：非法候选零回测。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from domain_sdk.factor_generation import FactorVocabulary, candidate_hash


class ReviewCode(StrEnum):
    MALFORMED_TREE = "malformed_tree"
    UNKNOWN_FIELD = "unknown_field"
    UNKNOWN_OPERATOR = "unknown_operator"
    UNKNOWN_WINDOW = "unknown_window"
    WINDOW_OUT_OF_BOUNDS = "window_out_of_bounds"
    REDUNDANT_IDENTITY = "redundant_identity"
    DIMENSION_MISMATCH = "dimension_mismatch"
    DEPTH_EXCEEDED = "depth_exceeded"
    COMPLEXITY_EXCEEDED = "complexity_exceeded"
    DUPLICATE = "duplicate"
    REVIEWER_REJECTED = "reviewer_rejected"


@dataclass(frozen=True, slots=True)
class OperatorSignature:
    """算子的量纲签名：接受的输入量纲（None=任意）与输出量纲（None=保持输入）。"""

    accepted_input_dimensions: frozenset[str] | None
    output_dimension: str | None
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class FactorDimensionTable:
    field_dimensions: Mapping[str, str]
    operator_signatures: Mapping[str, OperatorSignature]
    vocabulary: FactorVocabulary | None = None

    def __post_init__(self) -> None:
        if not self.field_dimensions or not all(
            isinstance(dimension, str) and dimension
            for dimension in self.field_dimensions.values()
        ):
            raise ValueError("every field must map to a non-empty dimension name")
        if not self.operator_signatures:
            raise ValueError("at least one operator signature is required")
        if self.vocabulary is None:
            return
        missing_dimensions = [
            field_name
            for field_name in self.vocabulary.fields
            if field_name not in self.field_dimensions
        ]
        if missing_dimensions:
            raise ValueError(
                f"dimension table is missing entries for vocabulary fields: {missing_dimensions}"
            )
        missing_signatures = [
            operator
            for operator in self.vocabulary.operators
            if operator not in self.operator_signatures
        ]
        if missing_signatures:
            raise ValueError(
                f"signature table is missing entries for vocabulary operators: {missing_signatures}"
            )


@dataclass(frozen=True, slots=True)
class FactorReviewPolicy:
    """审查硬门槛的 Profile Policy：树深度、复杂度、窗口边界与恒等式过滤。"""

    max_depth: int = 3
    max_operator_nodes: int = 4
    min_window: int = 2
    max_window: int = 250
    reject_redundant_identity: bool = True

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max depth must be positive")
        if self.max_operator_nodes < 1:
            raise ValueError("max operator nodes must be positive")
        if self.min_window < 1:
            raise ValueError("min window must be positive")
        if self.min_window > self.max_window:
            raise ValueError("min window must not exceed max window")


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    definition: Mapping[str, object]
    accepted: bool
    reasons: tuple[ReviewCode, ...]


@dataclass(frozen=True, slots=True)
class ReviewReport:
    outcomes: tuple[ReviewOutcome, ...]

    @property
    def accepted_definitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(outcome.definition for outcome in self.outcomes if outcome.accepted)

    @property
    def rejected(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.accepted)

    @property
    def rejection_summary(self) -> dict[ReviewCode, int]:
        summary: dict[ReviewCode, int] = {}
        for outcome in self.outcomes:
            for reason in outcome.reasons:
                summary[reason] = summary.get(reason, 0) + 1
        return summary


def _snapshot(definition: object) -> Mapping[str, object]:
    return dict(definition) if isinstance(definition, Mapping) else {}


def _is_wellformed(node: object) -> bool:
    """严格 AST：叶子只能是 ``{"field": str}``；算子节点只能是 op/window/input 三键。"""
    if not isinstance(node, Mapping):
        return False
    keys = set(node)
    if "field" in keys:
        return keys == {"field"} and isinstance(node["field"], str)
    if keys != {"op", "window", "input"}:
        return False
    if not isinstance(node["op"], str):
        return False
    if isinstance(node["window"], bool) or not isinstance(node["window"], int):
        return False
    return _is_wellformed(node["input"])


class CandidateReviewer:
    """审查硬门槛：结构 → 词表 → 边界 → 恒等式 → 量纲 → 深度/复杂度 → 去重。"""

    def __init__(
        self,
        vocabulary: FactorVocabulary,
        dimensions: FactorDimensionTable,
        *,
        policy: FactorReviewPolicy | None = None,
    ) -> None:
        missing_dimensions = [
            field_name
            for field_name in vocabulary.fields
            if field_name not in dimensions.field_dimensions
        ]
        if missing_dimensions:
            raise ValueError(
                f"dimension table is missing entries for vocabulary fields: {missing_dimensions}"
            )
        missing_signatures = [
            operator
            for operator in vocabulary.operators
            if operator not in dimensions.operator_signatures
        ]
        if missing_signatures:
            raise ValueError(
                f"signature table is missing entries for vocabulary operators: {missing_signatures}"
            )
        self._vocabulary = vocabulary
        self._dimensions = dimensions
        self._policy = policy if policy is not None else FactorReviewPolicy()

    @property
    def policy(self) -> FactorReviewPolicy:
        return self._policy

    def review(self, definition: object, *, known_hashes: Iterable[str] = ()) -> ReviewOutcome:
        """对单个候选给出确定性 verdict；同一输入永远同一结论。"""
        if not _is_wellformed(definition):
            return ReviewOutcome(_snapshot(definition), False, (ReviewCode.MALFORMED_TREE,))
        tree = cast(Mapping[str, object], definition)
        reasons = self._vocabulary_reasons(tree)
        if reasons:
            return ReviewOutcome(dict(tree), False, tuple(reasons))
        if self._policy.reject_redundant_identity and self._has_redundant_identity(tree):
            reasons.append(ReviewCode.REDUNDANT_IDENTITY)
        if self._has_dimension_mismatch(tree):
            reasons.append(ReviewCode.DIMENSION_MISMATCH)
        depth = self._operator_depth(tree)
        if depth > self._policy.max_depth:
            reasons.append(ReviewCode.DEPTH_EXCEEDED)
        if depth > self._policy.max_operator_nodes:
            reasons.append(ReviewCode.COMPLEXITY_EXCEEDED)
        if candidate_hash(tree) in frozenset(known_hashes):
            reasons.append(ReviewCode.DUPLICATE)
        return ReviewOutcome(dict(tree), not reasons, tuple(reasons))

    def review_batch(
        self,
        definitions: Iterable[object],
        *,
        known_hashes: Iterable[str] = (),
    ) -> ReviewReport:
        """批量审查；批内重复直接判 DUPLICATE，不重复展开审查。"""
        known = frozenset(known_hashes)
        seen: set[str] = set()
        outcomes: list[ReviewOutcome] = []
        for definition in definitions:
            if _is_wellformed(definition) and isinstance(definition, Mapping):
                digest = candidate_hash(definition)
            else:
                digest = None
            if digest is not None and digest in seen:
                outcomes.append(
                    ReviewOutcome(_snapshot(definition), False, (ReviewCode.DUPLICATE,))
                )
                continue
            if digest is not None:
                seen.add(digest)
            outcomes.append(self.review(definition, known_hashes=known))
        return ReviewReport(tuple(outcomes))

    def _vocabulary_reasons(self, definition: Mapping[str, object]) -> list[ReviewCode]:
        reasons: list[ReviewCode] = []

        def walk(node: Mapping[str, object]) -> None:
            if "field" in node:
                if str(node["field"]) not in self._vocabulary.fields:
                    reasons.append(ReviewCode.UNKNOWN_FIELD)
                return
            operator = str(node["op"])
            window = cast(int, node["window"])
            if operator not in self._vocabulary.operators:
                reasons.append(ReviewCode.UNKNOWN_OPERATOR)
            if window not in self._vocabulary.windows:
                reasons.append(ReviewCode.UNKNOWN_WINDOW)
            elif not self._policy.min_window <= window <= self._policy.max_window:
                reasons.append(ReviewCode.WINDOW_OUT_OF_BOUNDS)
            walk(node["input"])  # type: ignore[arg-type]

        walk(definition)
        return reasons

    def _has_redundant_identity(self, definition: Mapping[str, object]) -> bool:
        current: Mapping[str, object] = definition
        while "op" in current:
            child: Mapping[str, object] = current["input"]  # type: ignore[assignment]
            if "op" in child:
                signature = self._dimensions.operator_signatures[str(current["op"])]
                same_operator = str(child["op"]) == str(current["op"])
                same_window = cast(int, child["window"]) == cast(int, current["window"])
                if same_operator and (same_window or signature.idempotent):
                    return True
            current = child
        return False

    def _has_dimension_mismatch(self, definition: Mapping[str, object]) -> bool:
        mismatched = False

        def resolve(node: Mapping[str, object]) -> str:
            nonlocal mismatched
            if "field" in node:
                return self._dimensions.field_dimensions[str(node["field"])]
            signature = self._dimensions.operator_signatures[str(node["op"])]
            input_dimension = resolve(node["input"])  # type: ignore[arg-type]
            accepted = signature.accepted_input_dimensions
            if accepted is not None and input_dimension not in accepted:
                mismatched = True
            return signature.output_dimension if signature.output_dimension else input_dimension

        resolve(definition)
        return mismatched

    @staticmethod
    def _operator_depth(definition: Mapping[str, object]) -> int:
        depth = 0
        current: Mapping[str, object] = definition
        while "op" in current:
            depth += 1
            current = current["input"]  # type: ignore[assignment]
        return depth
