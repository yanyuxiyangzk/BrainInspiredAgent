"""L-006 tests: generation and review sub-agents as independent, schema-gated skills.

验收主断言是"隔离与 Schema"：两个 Sub-agent 拥有互不相同的 Capability/Manifest/
Binding，输入输出全部经 Schema 校验；审查 Sub-agent 的结构化 verdict 只能否决、
不能放行——硬门槛拒绝的候选恒被拒绝（merge_model_review 语义）。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.skills import (
    CapabilityRegistry,
    HealthStatus,
    SideEffect,
    SkillHealth,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from domain_sdk.factor_adaptation import AdaptiveFactorSearch, RoundFeedback
from domain_sdk.factor_generation import FactorVocabulary, ParentPool, candidate_hash
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    FactorReviewPolicy,
    OperatorSignature,
    ReviewCode,
)
from domain_sdk.factor_subagents import (
    GENERATION_CAPABILITY,
    MAX_REASONS,
    REVIEW_CAPABILITY,
    AgentVerdict,
    GenerationSubAgent,
    GovernedFactorReview,
    ReviewJudgement,
    ReviewSubAgent,
    SubAgentError,
    factor_subagent_capability_contracts,
    factor_subagent_manifests,
    factor_subagent_registrations,
)

NOW = datetime(2026, 9, 5, tzinfo=UTC)

VOCABULARY = FactorVocabulary(
    fields=("close", "high", "volume"),
    operators=("rank", "ts_mean", "ts_delta"),
    windows=(5, 10, 20),
)
DIMENSIONS = FactorDimensionTable(
    field_dimensions={"close": "price", "high": "price", "volume": "volume"},
    operator_signatures={
        "rank": OperatorSignature(
            accepted_input_dimensions=None, output_dimension="shapeless", idempotent=True
        ),
        "ts_mean": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
        "ts_delta": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
    },
)


def gate() -> CandidateReviewer:
    return CandidateReviewer(
        VOCABULARY, DIMENSIONS, policy=FactorReviewPolicy(min_window=5, max_window=20)
    )


class StaticGenerationPort:
    """确定性生成端口；记录收到的 count 以便隔离断言。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def propose(self, count: int) -> list[dict[str, object]]:
        self.calls.append(count)
        return [{"op": "ts_delta", "window": 20, "input": {"field": "close"}} for _ in range(count)]


class StubReviewPort:
    """可编程审查端口：固定 verdict，并记录请求以供隔离断言。"""

    def __init__(self, verdict: AgentVerdict = AgentVerdict.ACCEPT) -> None:
        self.verdict = verdict
        self.requests: list[Mapping[str, object]] = []

    async def review(self, definition: Mapping[str, object]) -> ReviewJudgement:
        self.requests.append(dict(definition))
        return ReviewJudgement(self.verdict, ("mechanism-check",))


def legal_tree() -> dict[str, object]:
    return {"op": "rank", "window": 10, "input": {"field": "close"}}


def hard_gate_violation() -> dict[str, object]:
    return {"op": "rank", "window": 15, "input": {"field": "close"}}  # 窗口不在词表内


# --------------------------------------------------------------------------- Schema 契约


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"count": 0},
        {"count": True},
        {"count": "4"},
        {"count": 65},  # 超出能力上限
        {"count": 2, "write_library": True},  # 越权字段：企图夹带副作用
    ],
)
async def test_generation_subagent_rejects_invalid_input(payload: dict[str, object]) -> None:
    port = StaticGenerationPort()
    agent = GenerationSubAgent(port)
    with pytest.raises(SubAgentError) as excinfo:
        await agent.invoke(payload)
    assert excinfo.value.code == "SUBAGENT_INPUT_INVALID"
    assert port.calls == []  # 非法输入从未触达模型端口


@pytest.mark.asyncio
async def test_generation_subagent_validates_output_shape() -> None:
    class BadPort:
        async def propose(self, count: int) -> list[object]:
            return ["rank(close)"]  # 候选必须是对象

    with pytest.raises(SubAgentError) as excinfo:
        await GenerationSubAgent(BadPort()).invoke({"count": 1})
    assert excinfo.value.code == "SUBAGENT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_generation_subagent_passes_valid_request() -> None:
    port = StaticGenerationPort()
    output = await GenerationSubAgent(port, max_count=8).invoke({"count": 3})
    assert port.calls == [3]
    assert len(output["candidates"]) == 3  # type: ignore[arg-type]
    assert output["candidates"][0]["op"] == "ts_delta"  # type: ignore[union-attr,index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"definition": legal_tree(), "extra": 1}, {}, {"definition": "rank(close)"}],
)
async def test_review_subagent_rejects_invalid_input(payload: dict[str, object]) -> None:
    port = StubReviewPort()
    with pytest.raises(SubAgentError) as excinfo:
        await ReviewSubAgent(port).invoke(payload)
    assert excinfo.value.code == "SUBAGENT_INPUT_INVALID"
    assert port.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["MAYBE", "", 42])
async def test_review_subagent_rejects_invalid_verdicts(verdict: object) -> None:
    class OddPort:
        async def review(self, definition: Mapping[str, object]) -> ReviewJudgement:
            return ReviewJudgement(verdict, ())  # type: ignore[arg-type]

    with pytest.raises(SubAgentError) as excinfo:
        await ReviewSubAgent(OddPort()).invoke({"definition": legal_tree()})
    assert excinfo.value.code == "SUBAGENT_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_review_subagent_caps_reason_count() -> None:
    class FloodPort:
        async def review(self, definition: Mapping[str, object]) -> ReviewJudgement:
            return ReviewJudgement(AgentVerdict.REJECT, ("r",) * (MAX_REASONS + 1))

    with pytest.raises(SubAgentError) as excinfo:
        await ReviewSubAgent(FloodPort()).invoke({"definition": legal_tree()})
    assert excinfo.value.code == "SUBAGENT_OUTPUT_INVALID"
    capped = ReviewJudgement(AgentVerdict.REJECT, ("r",) * MAX_REASONS)
    output = await ReviewSubAgent(_ExactPort(capped)).invoke({"definition": legal_tree()})
    assert len(output["reasons"]) == MAX_REASONS  # type: ignore[arg-type]


class _ExactPort:
    def __init__(self, judgement: ReviewJudgement) -> None:
        self._judgement = judgement

    async def review(self, definition: Mapping[str, object]) -> ReviewJudgement:
        return self._judgement


# --------------------------------------------------------------------------- 隔离


@pytest.mark.asyncio
async def test_subagents_never_touch_each_others_ports() -> None:
    generation_port = StaticGenerationPort()
    review_port = StubReviewPort()
    generation = GenerationSubAgent(generation_port)
    review = ReviewSubAgent(review_port)
    await generation.invoke({"count": 2})
    await review.invoke({"definition": legal_tree()})
    assert generation_port.calls == [2]
    assert len(review_port.requests) == 1
    # 各自独立：互不调用对方的端口
    assert review_port.requests[0] == legal_tree()


@pytest.mark.asyncio
async def test_input_mutation_after_invoke_does_not_leak() -> None:
    agent = GenerationSubAgent(StaticGenerationPort())
    payload: dict[str, object] = {"count": 1}
    first = await agent.invoke(payload)
    payload["count"] = 99
    payload["injected"] = True
    second = await agent.invoke({"count": 1})
    assert len(first["candidates"]) == 1  # type: ignore[arg-type]
    assert len(second["candidates"]) == 1  # type: ignore[arg-type]
    assert "injected" not in second


def test_subagent_manifests_are_independent_and_loadable() -> None:
    manifests = factor_subagent_manifests()
    generation, review = manifests
    assert generation["skill_id"] != review["skill_id"]
    assert generation["digest"] != review["digest"]
    provided = {
        provision["capability"]
        for manifest in manifests
        for provision in manifest["provides"]  # type: ignore[union-attr]
    }
    assert provided == {GENERATION_CAPABILITY, REVIEW_CAPABILITY}


def test_subagents_resolve_to_independent_bindings() -> None:
    capabilities = CapabilityRegistry()
    for contract in factor_subagent_capability_contracts():
        capabilities.register(contract)
    skills = SkillRegistry(capabilities)
    for manifest in factor_subagent_manifests():
        installed = skills.install(manifest, package_digest=str(manifest["digest"]))
        verified = skills.verify(installed.manifest.skill_id, installed.manifest.version)
        skills.enable(
            verified.manifest.skill_id,
            verified.manifest.version,
            SkillHealth(HealthStatus.HEALTHY, NOW),
        )
    resolver = SkillResolver(capabilities, skills, clock=FakeClock(NOW))
    generation_binding = resolver.resolve(
        SkillRequirement("propose-node", GENERATION_CAPABILITY, "1.0", frozenset(), SideEffect.PURE),
        policy_version="policy-1",
    )
    review_binding = resolver.resolve(
        SkillRequirement("review-node", REVIEW_CAPABILITY, "1.0", frozenset(), SideEffect.PURE),
        policy_version="policy-1",
    )
    assert generation_binding.skill_id != review_binding.skill_id
    assert generation_binding.capability != review_binding.capability
    assert generation_binding.skill_digest != review_binding.skill_digest
    assert generation_binding.node_id == "propose-node"
    assert review_binding.node_id == "review-node"


def test_domain_registrations_carry_distinct_capability_contracts() -> None:
    registrations = factor_subagent_registrations(
        GenerationSubAgent(StaticGenerationPort()), ReviewSubAgent(StubReviewPort())
    )
    assert len(registrations) == 2
    capabilities = {registration.manifest.capabilities[0] for registration in registrations}
    assert capabilities == {GENERATION_CAPABILITY, REVIEW_CAPABILITY}
    contracts = factor_subagent_capability_contracts()
    assert all(contract.side_effect.value == "PURE" for contract in contracts)


# --------------------------------------------------------------------------- 审查不得绕过硬门槛


@pytest.mark.asyncio
async def test_review_agent_accept_cannot_overturn_hard_gate() -> None:
    governed = GovernedFactorReview(gate(), ReviewSubAgent(StubReviewPort(AgentVerdict.ACCEPT)))
    outcome = await governed.review(hard_gate_violation())
    assert not outcome.accepted  # 模型说 ACCEPT，硬门槛仍然否决
    assert ReviewCode.UNKNOWN_WINDOW in outcome.reasons
    assert outcome.agent_verdict == "ACCEPT"


@pytest.mark.asyncio
async def test_review_agent_can_veto_hard_accepted_candidate() -> None:
    governed = GovernedFactorReview(gate(), ReviewSubAgent(StubReviewPort(AgentVerdict.REJECT)))
    outcome = await governed.review(legal_tree())
    assert not outcome.accepted  # 模型不能放行，但可以否决硬门槛通过的候选
    assert ReviewCode.REVIEWER_REJECTED in outcome.reasons
    assert outcome.agent_reasons == ("mechanism-check",)


@pytest.mark.asyncio
async def test_review_agent_failure_degrades_to_gate_only() -> None:
    class BrokenPort:
        async def review(self, definition: Mapping[str, object]) -> ReviewJudgement:
            raise RuntimeError("model offline")

    governed = GovernedFactorReview(gate(), ReviewSubAgent(BrokenPort()))
    outcome = await governed.review(legal_tree())
    assert outcome.accepted
    assert outcome.agent_verdict is None
    assert outcome.agent_reasons == ()


@pytest.mark.asyncio
async def test_governed_review_feeds_an_adaptive_search_round() -> None:
    """与 L-004/L-005 组合：全流程中非法候选零回测且审查 verdict 只作参考。"""
    search = AdaptiveFactorSearch(VOCABULARY, seed=41)
    port = StubReviewPort(AgentVerdict.ACCEPT)
    governed = GovernedFactorReview(gate(), ReviewSubAgent(port))
    pool = ParentPool(capacity=16)
    pool.add({"op": "rank", "window": 10, "input": {"field": "close"}})
    generated = await search.run_round(10, pool)
    backtested: list[Mapping[str, object]] = []
    tested: set[str] = set()
    accepted: set[str] = set()
    for item in generated.candidates:
        verdict = await governed.review(item.definition)
        if verdict.accepted:
            digest = candidate_hash(item.definition)
            tested.add(digest)
            backtested.append(item.definition)
            if item.definition.get("op") == "rank" and item.definition.get("window") == 10:
                accepted.add(digest)
                pool.add(item.definition)
    assert backtested
    assert search.observe(RoundFeedback.from_round(generated, tested, accepted)).rounds == 1
