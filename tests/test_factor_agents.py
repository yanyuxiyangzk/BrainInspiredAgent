"""L-006 tests: generator/reviewer sub-agents with independent, isolated SkillBindings.

架构 §3.2：生成 Agent 与审查 Agent 必须使用不同 SkillBinding、模型配置和上下文；
LLM 审查只返回结构化 verdict，不能绕过硬门槛，更不能触发回测。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from active_agent_platform.skills import SkillBinding
from domain_sdk.factor_agents import (
    FactorAgentRole,
    FactorSubAgentPair,
    FactorSubAgentSpec,
    ModelReviewVerdict,
    merge_model_review,
)
from domain_sdk.factor_review import FactorDimensionTable, OperatorSignature, ReviewCode

ROOT = Path(__file__).parents[1] / "schemas"
STAMP = "2026-09-05T15:00:00Z"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

BINDING_SCHEMA = json.loads((ROOT / "skill" / "skill-binding-1.0.schema.json").read_text("utf-8"))


def binding(
    *,
    node_id: str = "factor.generate",
    capability: str = "factor.generation",
    skill_id: str = "factor-generator",
    digest: str = DIGEST_A,
) -> SkillBinding:
    return SkillBinding(
        node_id=node_id,
        capability=capability,
        capability_version="1.0",
        skill_id=skill_id,
        skill_version="1.0.0",
        skill_digest=digest,
        binding_policy_version="policy/1",
        resolved_at=_stamp(),
    )


def _stamp() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 9, 5, 15, 0, 0, tzinfo=UTC)


def generator_spec(**overrides: Any) -> FactorSubAgentSpec:
    kwargs: dict[str, Any] = {
        "binding": binding(),
        "model_config": {"model": "reasoner-x", "temperature": 0.7},
        "context_namespace": "factor.generation",
    }
    kwargs.update(overrides)
    return FactorSubAgentSpec(FactorAgentRole.GENERATOR, **kwargs)


def reviewer_spec(**overrides: Any) -> FactorSubAgentSpec:
    kwargs: dict[str, Any] = {
        "binding": binding(
            node_id="factor.review",
            capability="factor.review",
            skill_id="factor-reviewer",
            digest=DIGEST_B,
        ),
        "model_config": {"model": "critic-y", "temperature": 0.1},
        "context_namespace": "factor.review",
    }
    kwargs.update(overrides)
    return FactorSubAgentSpec(FactorAgentRole.REVIEWER, **kwargs)


def pair() -> FactorSubAgentPair:
    return FactorSubAgentPair(generator=generator_spec(), reviewer=reviewer_spec())


# --------------------------------------------------------------------------- Schema


def test_bindings_validate_against_skill_binding_schema() -> None:
    validator = Draft202012Validator(BINDING_SCHEMA, format_checker=FormatChecker())
    for spec in (generator_spec(), reviewer_spec()):
        document = spec.to_binding_document()
        validator.validate(document)
        assert document["skill_id"] in {"factor-generator", "factor-reviewer"}


def test_binding_document_rejects_schema_violations() -> None:
    validator = Draft202012Validator(BINDING_SCHEMA, format_checker=FormatChecker())
    document = reviewer_spec().to_binding_document()
    broken = copy.deepcopy(document)
    del broken["skill_digest"]
    with pytest.raises(ValidationError):
        validator.validate(broken)


# --------------------------------------------------------------------------- 隔离


def test_pair_enforces_full_isolation() -> None:
    gate = pair()
    assert gate.generator.binding.capability == "factor.generation"
    assert gate.reviewer.binding.capability == "factor.review"
    assert gate.generator.binding.skill_id != gate.reviewer.binding.skill_id
    assert gate.generator.binding.skill_digest != gate.reviewer.binding.skill_digest
    assert gate.generator.context_namespace != gate.reviewer.context_namespace
    assert gate.generator.model_config != gate.reviewer.model_config


@pytest.mark.parametrize("field", ["skill_id", "skill_digest", "node_id"])
def test_pair_rejects_shared_binding_identity(field: str) -> None:
    reviewer_kwargs: dict[str, Any] = {}
    source = generator_spec().binding
    reviewer_kwargs["binding"] = SkillBinding(
        node_id=source.node_id if field == "node_id" else "factor.review",
        capability="factor.review",
        capability_version=source.capability_version,
        skill_id=source.skill_id if field == "skill_id" else "factor-reviewer",
        skill_version=source.skill_version,
        skill_digest=source.skill_digest if field == "skill_digest" else DIGEST_B,
        binding_policy_version=source.binding_policy_version,
        resolved_at=_stamp(),
    )
    with pytest.raises(ValueError, match="isolat|binding"):
        FactorSubAgentPair(generator=generator_spec(), reviewer=reviewer_spec(**reviewer_kwargs))


def test_pair_rejects_shared_model_config_or_namespace() -> None:
    with pytest.raises(ValueError, match="model"):
        FactorSubAgentPair(
            generator=generator_spec(),
            reviewer=reviewer_spec(model_config={"model": "reasoner-x", "temperature": 0.7}),
        )
    with pytest.raises(ValueError, match="context"):
        FactorSubAgentPair(
            generator=generator_spec(),
            reviewer=reviewer_spec(context_namespace="factor.generation"),
        )


def test_spec_capability_must_match_role() -> None:
    with pytest.raises(ValueError, match="capability"):
        FactorSubAgentSpec(
            FactorAgentRole.GENERATOR,
            binding=binding(capability="factor.review"),
            model_config={"model": "m"},
            context_namespace="factor.generation",
        )


# --------------------------------------------------------------------------- 结构化 verdict 与硬门槛合并


def test_model_verdict_roundtrip_and_validation() -> None:
    verdict = ModelReviewVerdict(True, 0.8, ())
    restored = ModelReviewVerdict.from_dict(verdict.to_dict())
    assert restored == verdict
    with pytest.raises(ValueError, match="confidence"):
        ModelReviewVerdict.from_dict({"accepts": True, "confidence": 1.5, "concerns": []})
    with pytest.raises(TypeError, match="accepts"):
        ModelReviewVerdict.from_dict({"confidence": 0.5, "concerns": []})
    with pytest.raises(TypeError, match="concerns"):
        ModelReviewVerdict.from_dict({"accepts": False, "confidence": 0.5, "concerns": "x"})


def _hard_outcome(accepted: bool) -> Any:
    from domain_sdk.factor_review import ReviewOutcome

    definition = {"field": "close"}
    reasons: tuple[ReviewCode, ...] = () if accepted else (ReviewCode.UNKNOWN_FIELD,)
    return ReviewOutcome(definition, accepted, reasons)


def test_model_can_reject_but_never_accept_past_hard_gate() -> None:
    from domain_sdk.factor_review import ReviewOutcome

    legal = _hard_outcome(True)
    upheld = merge_model_review(legal, ModelReviewVerdict(True, 0.9, ()))
    assert upheld.accepted and upheld.reasons == ()

    vetoed = merge_model_review(legal, ModelReviewVerdict(False, 0.9, ("极值敏感",)))
    assert not vetoed.accepted
    assert ReviewCode.REVIEWER_REJECTED in vetoed.reasons

    illegal = _hard_outcome(False)
    bypass = merge_model_review(illegal, ModelReviewVerdict(True, 0.99, ()))
    assert not bypass.accepted  # LLM 不能推翻硬门槛
    assert ReviewCode.UNKNOWN_FIELD in bypass.reasons
    assert isinstance(bypass, ReviewOutcome)


def test_dimension_table_ready_for_agent_wiring() -> None:
    table = FactorDimensionTable(
        field_dimensions={"close": "price"},
        operator_signatures={
            "rank": OperatorSignature(accepted_input_dimensions=None, output_dimension=None)
        },
    )
    assert table.field_dimensions["close"] == "price"
