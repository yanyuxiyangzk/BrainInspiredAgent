"""Generation and review sub-agents as independent, schema-gated skills (L-006).

架构 §6：生成与审查 Sub-agent 都是独立 Skill 调用，拥有独立 Capability、
Manifest、Binding、Schema 和上下文；它们不持有任何写端口（checkpoint、
Registry、因子库对其不可达），结果只返回给父流程接受硬校验和事务提交。
审查 Sub-agent 的结构化 verdict 经 `merge_model_review` 与硬门槛（L-005
CandidateReviewer）合并：模型只能否决、不能放行，硬门槛拒绝的候选恒被拒绝。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from active_agent_platform.skills import CapabilityContract, SideEffect
from domain_sdk.contracts import JsonValue, SkillManifest, SkillRegistration
from domain_sdk.factor_agents import ModelReviewVerdict, merge_model_review
from domain_sdk.factor_review import CandidateReviewer, ReviewOutcome

GENERATION_CAPABILITY = "factor.generation"
REVIEW_CAPABILITY = "factor.review"
GENERATION_SKILL_ID = "factor-generation-agent"
REVIEW_SKILL_ID = "factor-review-agent"
SUBAGENT_VERSION = "1.0.0"
MAX_GENERATION_COUNT = 64
MAX_REASONS = 16


class SubAgentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _object_schema(
    properties: Mapping[str, object], required: Sequence[str]
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


GENERATION_INPUT_SCHEMA = _object_schema(
    {
        "count": {"type": "integer", "minimum": 1, "maximum": MAX_GENERATION_COUNT},
    },
    ("count",),
)
GENERATION_OUTPUT_SCHEMA = _object_schema(
    {
        "candidates": {
            "type": "array",
            "items": {"type": "object"},
            "maxItems": MAX_GENERATION_COUNT,
        },
    },
    ("candidates",),
)
REVIEW_INPUT_SCHEMA = _object_schema(
    {"definition": {"type": "object"}},
    ("definition",),
)
REVIEW_OUTPUT_SCHEMA = _object_schema(
    {
        "verdict": {"type": "string", "enum": ["ACCEPT", "REJECT"]},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_REASONS},
        "notes": {"type": "string"},
    },
    ("verdict", "reasons"),
)


def _validate(schema: Mapping[str, object], payload: Mapping[str, object], code: str) -> None:
    errors = list(Draft202012Validator(schema).iter_errors(payload))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        detail = f"{location}: {first.message}" if location else first.message
        raise SubAgentError(code, detail)


class GenerationPort(Protocol):
    """机制提案端口（生成 Sub-agent 背后的模型调用边界）。"""

    async def propose(self, count: int) -> Sequence[Mapping[str, object]]: ...


class AgentVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class ReviewJudgement:
    verdict: AgentVerdict
    reasons: tuple[str, ...] = ()
    notes: str | None = None


class ReviewPort(Protocol):
    """审查端口（审查 Sub-agent 背后的模型调用边界）。"""

    async def review(self, definition: Mapping[str, object]) -> ReviewJudgement: ...


class GenerationSubAgent:
    """生成 Sub-agent：双向 Schema 校验；除提案端口外不持任何端口。"""

    def __init__(self, port: GenerationPort, *, max_count: int = MAX_GENERATION_COUNT) -> None:
        if not 1 <= max_count <= MAX_GENERATION_COUNT:
            raise ValueError(f"max_count must stay within [1, {MAX_GENERATION_COUNT}]")
        self._port = port
        self._max_count = max_count
        self.input_schema: dict[str, object] = _object_schema(
            {"count": {"type": "integer", "minimum": 1, "maximum": max_count}}, ("count",)
        )
        self.output_schema: dict[str, object] = _object_schema(
            {
                "candidates": {"type": "array", "items": {"type": "object"}, "maxItems": max_count}
            },
            ("candidates",),
        )

    @property
    def capability(self) -> str:
        return GENERATION_CAPABILITY

    async def invoke(self, input_data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        _validate(self.input_schema, input_data, "SUBAGENT_INPUT_INVALID")
        count = cast(int, input_data["count"])
        proposed = list(await self._port.propose(count))
        candidates: list[JsonValue] = [
            dict(cast("Mapping[str, JsonValue]", item)) if isinstance(item, Mapping) else item
            for item in proposed
        ]
        output: dict[str, JsonValue] = {"candidates": candidates}
        _validate(self.output_schema, output, "SUBAGENT_OUTPUT_INVALID")
        return output


class ReviewSubAgent:
    """审查 Sub-agent：结构化 verdict 进出；除审查端口外不持任何端口。"""

    def __init__(self, port: ReviewPort) -> None:
        self._port = port

    @property
    def capability(self) -> str:
        return REVIEW_CAPABILITY

    async def invoke(self, input_data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        _validate(REVIEW_INPUT_SCHEMA, input_data, "SUBAGENT_INPUT_INVALID")
        definition = cast(Mapping[str, JsonValue], input_data["definition"])
        judgement = await self._port.review(definition)
        try:
            verdict = AgentVerdict(judgement.verdict)
        except ValueError as error:
            raise SubAgentError(
                "SUBAGENT_OUTPUT_INVALID",
                f"verdict is not a structured enum value: {judgement.verdict!r}",
            ) from error
        output: dict[str, JsonValue] = {
            "verdict": verdict.value,
            "reasons": [str(reason) for reason in judgement.reasons],
        }
        if judgement.notes is not None:
            output["notes"] = str(judgement.notes)
        _validate(REVIEW_OUTPUT_SCHEMA, output, "SUBAGENT_OUTPUT_INVALID")
        return output


@dataclass(frozen=True, slots=True)
class GovernedReviewOutcome:
    """合并后的裁决：硬门槛不可被模型放行；模型 REJECT 追加 REVIEWER_REJECTED。"""

    gate: ReviewOutcome
    agent_verdict: str | None
    agent_reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.gate.accepted

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.gate.reasons


class GovernedFactorReview:
    """模型审查只能否决，不能放行：最终裁决由硬门槛与结构化 verdict 合并给出。

    硬门槛拒绝的候选恒被拒绝；模型 REJECT 会以 ``REVIEWER_REJECTED`` 追加否决；
    模型失败或降级时直接回落到纯硬门槛结论。
    """

    def __init__(self, gate: CandidateReviewer, agent: ReviewSubAgent) -> None:
        self._gate = gate
        self._agent = agent

    async def review(
        self, definition: object, *, known_hashes: Iterable[str] = ()
    ) -> GovernedReviewOutcome:
        gate_outcome = self._gate.review(definition, known_hashes=known_hashes)
        agent_verdict: str | None = None
        agent_reasons: tuple[str, ...] = ()
        if isinstance(definition, Mapping):
            try:
                raw = await self._agent.invoke({"definition": dict(definition)})
                agent_verdict = str(raw["verdict"])
                agent_reasons = tuple(str(reason) for reason in cast(list[str], raw["reasons"]))
            except Exception:  # noqa: BLE001 - 模型降级不得改变硬门槛裁决
                agent_verdict, agent_reasons = None, ()
        merged = gate_outcome
        if agent_verdict is not None:
            merged = merge_model_review(
                gate_outcome,
                ModelReviewVerdict(
                    accepts=agent_verdict != AgentVerdict.REJECT.value,
                    confidence=1.0,
                    concerns=agent_reasons,
                ),
            )
        return GovernedReviewOutcome(merged, agent_verdict, agent_reasons)


def _skill_digest(skill_id: str, version: str) -> str:
    payload = f"bia-factor-subagent:{skill_id}@{version}"
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def factor_subagent_capability_contracts() -> tuple[CapabilityContract, CapabilityContract]:
    """两个 Sub-agent 的能力契约：同名空间、互不相同、全部 PURE。"""
    return (
        CapabilityContract(
            GENERATION_CAPABILITY,
            "1.0",
            GENERATION_INPUT_SCHEMA,
            GENERATION_OUTPUT_SCHEMA,
            SideEffect.PURE,
        ),
        CapabilityContract(
            REVIEW_CAPABILITY,
            "1.0",
            REVIEW_INPUT_SCHEMA,
            REVIEW_OUTPUT_SCHEMA,
            SideEffect.PURE,
        ),
    )


def factor_subagent_manifests() -> tuple[dict[str, object], dict[str, object]]:
    """平台可安装的 Manifest；两个 Skill 的身份、digest 与能力互不相同。"""
    specifications = (
        (
            GENERATION_SKILL_ID,
            GENERATION_CAPABILITY,
            "domain_sdk.factor_subagents:GenerationSubAgent",
        ),
        (REVIEW_SKILL_ID, REVIEW_CAPABILITY, "domain_sdk.factor_subagents:ReviewSubAgent"),
    )
    manifests: list[dict[str, object]] = []
    for skill_id, capability, entrypoint in specifications:
        manifests.append(
            {
                "schema_version": "1.0",
                "skill_id": skill_id,
                "version": SUBAGENT_VERSION,
                "digest": _skill_digest(skill_id, SUBAGENT_VERSION),
                "provides": [{"capability": capability, "capability_version": "1.0"}],
                "side_effect": "PURE",
                "required_permissions": [],
                "runtime": "python",
                "entrypoint": entrypoint,
                "timeout_seconds": 30,
                "concurrency_limit": 4,
                "supports_cancel": True,
                "supports_query": False,
                "resources": {"max_cost": 0.0, "max_latency_ms": 30_000, "memory_mb": 64},
            }
        )
    return (manifests[0], manifests[1])


def factor_subagent_registrations(
    generation: GenerationSubAgent, review: ReviewSubAgent
) -> tuple[SkillRegistration, SkillRegistration]:
    """域级注册：两个 Sub-agent 各自持有独立 Manifest 与 Adapter。"""
    generation_manifest, review_manifest = factor_subagent_manifests()
    return (
        SkillRegistration(
            manifest=SkillManifest(
                skill_id=str(generation_manifest["skill_id"]),
                version=str(generation_manifest["version"]),
                digest=str(generation_manifest["digest"]),
                capabilities=(GENERATION_CAPABILITY,),
            ),
            adapter=generation,
        ),
        SkillRegistration(
            manifest=SkillManifest(
                skill_id=str(review_manifest["skill_id"]),
                version=str(review_manifest["version"]),
                digest=str(review_manifest["digest"]),
                capabilities=(REVIEW_CAPABILITY,),
            ),
            adapter=review,
        ),
    )
