"""Generator/reviewer sub-agents with independent isolated SkillBindings (L-006).

架构 §3.2：生成 Agent 与审查 Agent 使用不同 SkillBinding、模型配置和上下文；
审查 Sub-agent 只返回结构化 verdict——``merge_model_review`` 保证硬门槛不可被
模型推翻，verdict 之外没有任何触发回测或写库的通道。绑定文档对
``skill-binding-1.0.schema.json`` 校验通过。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from active_agent_platform.skills import SkillBinding
from domain_sdk.factor_review import ReviewCode, ReviewOutcome


class FactorAgentRole(StrEnum):
    GENERATOR = "generator"
    REVIEWER = "reviewer"

    def required_capability(self) -> str:
        return _ROLE_CAPABILITIES[self]


_ROLE_CAPABILITIES: Mapping[FactorAgentRole, str] = {
    FactorAgentRole.GENERATOR: "factor.generation",
    FactorAgentRole.REVIEWER: "factor.review",
}


@dataclass(frozen=True, slots=True)
class FactorSubAgentSpec:
    """一个 Sub-agent 的身份：SkillBinding + 模型配置 + 上下文命名空间。"""

    role: FactorAgentRole
    binding: SkillBinding
    model_config: Mapping[str, object]
    context_namespace: str

    def __post_init__(self) -> None:
        expected = self.role.required_capability()
        if self.binding.capability != expected:
            raise ValueError(
                f"{self.role.value} binding capability must be {expected!r}, "
                f"got {self.binding.capability!r}"
            )
        if not isinstance(self.model_config, Mapping) or "model" not in self.model_config:
            raise ValueError("model_config must declare at least a model")
        if not self.context_namespace:
            raise ValueError("context_namespace must be non-empty")

    def to_binding_document(self) -> dict[str, object]:
        """``skill-binding-1.0.schema.json`` 兼容的绑定文档。"""
        return {
            "schema_version": "1.0",
            "node_id": self.binding.node_id,
            "capability": self.binding.capability,
            "capability_version": self.binding.capability_version,
            "skill_id": self.binding.skill_id,
            "skill_version": self.binding.skill_version,
            "skill_digest": self.binding.skill_digest,
            "binding_policy_version": self.binding.binding_policy_version,
            "resolved_at": self.binding.resolved_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class FactorSubAgentPair:
    """生成/审查 Sub-agent 对；构造时强制三维隔离（绑定、模型、上下文）。"""

    generator: FactorSubAgentSpec
    reviewer: FactorSubAgentSpec

    def __post_init__(self) -> None:
        if self.generator.role is not FactorAgentRole.GENERATOR or (
            self.reviewer.role is not FactorAgentRole.REVIEWER
        ):
            raise ValueError("pair roles must be generator and reviewer")
        generated, reviewed = self.generator.binding, self.reviewer.binding
        shared = {
            name
            for name in ("node_id", "skill_id", "skill_digest")
            if getattr(generated, name) == getattr(reviewed, name)
        }
        if shared:
            raise ValueError(f"generator and reviewer bindings must be isolated: {sorted(shared)}")
        if dict(self.generator.model_config) == dict(self.reviewer.model_config):
            raise ValueError("generator and reviewer model configs must be isolated")
        if self.generator.context_namespace == self.reviewer.context_namespace:
            raise ValueError("generator and reviewer contexts must be isolated")


@dataclass(frozen=True, slots=True)
class ModelReviewVerdict:
    """审查 Sub-agent 唯一允许的输出：结构化 verdict，别无通道。"""

    accepts: bool
    confidence: float
    concerns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must stay within [0, 1]")

    def to_dict(self) -> dict[str, object]:
        return {
            "accepts": self.accepts,
            "confidence": self.confidence,
            "concerns": list(self.concerns),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ModelReviewVerdict:
        accepts = payload.get("accepts")
        if not isinstance(accepts, bool):
            raise TypeError("verdict accepts flag is required")
        confidence = payload.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise TypeError("verdict confidence must be a number")
        concerns = payload.get("concerns", [])
        if not isinstance(concerns, list) or any(not isinstance(item, str) for item in concerns):
            raise TypeError("verdict concerns must be a list of strings")
        return cls(accepts, float(confidence), tuple(concerns))


def merge_model_review(
    hard_outcome: ReviewOutcome, verdict: ModelReviewVerdict
) -> ReviewOutcome:
    """模型审查只能否决，不能放行：硬门槛拒绝的候选恒被拒绝。"""
    if not hard_outcome.accepted:
        return hard_outcome
    if verdict.accepts:
        return hard_outcome
    return ReviewOutcome(
        hard_outcome.definition,
        False,
        hard_outcome.reasons + (ReviewCode.REVIEWER_REJECTED,),
    )
