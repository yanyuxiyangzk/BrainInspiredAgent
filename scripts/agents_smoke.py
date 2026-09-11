"""Post-install smoke for L-006: isolated sub-agent bindings; model can veto only."""
from datetime import UTC, datetime

from active_agent_platform.skills import SkillBinding
from domain_sdk.factor_agents import (
    FactorAgentRole,
    FactorSubAgentPair,
    FactorSubAgentSpec,
    ModelReviewVerdict,
    merge_model_review,
)
from domain_sdk.factor_generation import FactorVocabulary
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    OperatorSignature,
    ReviewCode,
)

NOW = datetime(2026, 9, 5, 15, 0, 0, tzinfo=UTC)
VOCAB = FactorVocabulary(
    fields=("close", "volume"), operators=("rank", "ts_mean", "ts_delta"), windows=(5, 10, 20)
)


def _binding(role: FactorAgentRole, skill_id: str, digest: str) -> SkillBinding:
    return SkillBinding(
        node_id=f"factor.{role.value}",
        capability=role.required_capability(),
        capability_version="1.0",
        skill_id=skill_id,
        skill_version="1.0.0",
        skill_digest=digest,
        binding_policy_version="policy/1",
        resolved_at=NOW,
    )


generator = FactorSubAgentSpec(
    FactorAgentRole.GENERATOR,
    binding=_binding(FactorAgentRole.GENERATOR, "factor-generator", "sha256:" + "a" * 64),
    model_config={"model": "reasoner-x", "temperature": 0.7},
    context_namespace="factor.generation",
)
reviewer = FactorSubAgentSpec(
    FactorAgentRole.REVIEWER,
    binding=_binding(FactorAgentRole.REVIEWER, "factor-reviewer", "sha256:" + "b" * 64),
    model_config={"model": "critic-y", "temperature": 0.1},
    context_namespace="factor.review",
)

pair = FactorSubAgentPair(generator=generator, reviewer=reviewer)
assert pair.generator.binding.capability != pair.reviewer.binding.capability

gate = CandidateReviewer(
    VOCAB,
    FactorDimensionTable(
        field_dimensions={"close": "price", "volume": "volume"},
        operator_signatures={
            "rank": OperatorSignature(
                accepted_input_dimensions=None, output_dimension="shapeless", idempotent=True
            ),
            "ts_mean": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
            "ts_delta": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
        },
    ),
)

# 硬门槛拒绝的候选：模型 verdict 不能放行
hard_reject = gate.review({"op": "wavelet", "window": 10, "input": {"field": "close"}})
merged = merge_model_review(hard_reject, ModelReviewVerdict(True, 1.0, ()))
assert not merged.accepted, "model must never overturn the hard gate"

# 硬门槛放行的候选：模型 verdict 仍可否决
hard_accept = gate.review({"op": "rank", "window": 10, "input": {"field": "close"}})
vetoed = merge_model_review(hard_accept, ModelReviewVerdict(False, 0.9, ("extreme sensitivity",)))
assert not vetoed.accepted and ReviewCode.REVIEWER_REJECTED in vetoed.reasons

print(
    "WSL packaging smoke PASS: isolated pair roles",
    pair.generator.role.value,
    "/",
    pair.reviewer.role.value,
)
