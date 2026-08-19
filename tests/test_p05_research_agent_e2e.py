from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import (
    BrainMode,
    BrainState,
    CancellationToken,
    CapabilityContract,
    CapabilityRegistry,
    CognitiveCoordinator,
    CompletionCondition,
    ConditionOperator,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    GovernedCognitiveApp,
    HealthStatus,
    LocalArtifactStore,
    MarketPhase,
    MemoryContextSnapshot,
    MotorExec,
    OutcomeEvaluator,
    OutcomePolicy,
    PlanningRule,
    PlanValidator,
    ResourceBudget,
    RiskBudget,
    RiskGate,
    RiskPolicy,
    RulePlanner,
    SideEffect,
    SkillContext,
    SkillHealth,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
    TraceQuery,
    WorkflowRegistry,
    WorkflowRuntime,
    WorkflowStatus,
    Workload,
    WorldModel,
)
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import DomainSkillBridge

NOW = datetime(2026, 8, 18, 9, tzinfo=UTC)


class Logger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields


def _research() -> tuple[object, object, str, dict[str, object]]:
    root = Path(__file__).parents[1] / "examples" / "research_agent"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research_agent import RESEARCH_CAPABILITY, RESEARCH_WORKFLOW, ExtractKeywords
    return ExtractKeywords, RESEARCH_CAPABILITY, "research-keywords", RESEARCH_WORKFLOW


@pytest.mark.asyncio
async def test_independent_research_domain_runs_cognition_to_outcome_and_trace(tmp_path: Path) -> None:
    extract_type, capability_name, skill_id, workflow_document = _research()
    assert isinstance(capability_name, str) and isinstance(skill_id, str)
    assert isinstance(workflow_document, dict)
    clock = FakeClock(NOW)
    identifiers = FakeUuidGenerator(UUID(int=index) for index in range(1, 300))
    database = SQLiteDatabase(tmp_path / "research.db")
    await database.initialize()
    workflows = WorkflowRegistry()
    registered = workflows.register(workflow_document, status=WorkflowStatus.VALIDATED)
    workflow = workflows.activate(registered.workflow_id, registered.version)
    capabilities = CapabilityRegistry()
    capabilities.register(CapabilityContract(
        capability_name, "1.0",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        {"type": "object", "properties": {"keywords": {"type": "array"}}, "required": ["keywords"]},
        SideEffect.PURE,
    ))
    skills = SkillRegistry(capabilities)
    digest = "sha256:" + "1" * 64
    skills.install({
        "schema_version": "1.0", "skill_id": skill_id, "version": "1.0.0",
        "digest": digest,
        "provides": [{"capability": capability_name, "capability_version": "1.0"}],
        "side_effect": "PURE", "required_permissions": [], "runtime": "python",
        "entrypoint": "research_agent:ExtractKeywords", "timeout_seconds": 10,
        "concurrency_limit": 1,
    }, package_digest=digest)
    skills.verify(skill_id, "1.0.0")
    skills.enable(skill_id, "1.0.0", SkillHealth(HealthStatus.HEALTHY, NOW, 0))
    binding = SkillResolver(capabilities, skills, clock=clock).resolve(SkillRequirement(
        "keywords", capability_name, "1.0", frozenset(), SideEffect.PURE,
    ), policy_version="research-policy/1")
    bindings = {(workflow.workflow_id, workflow.version, "keywords"): binding}
    artifacts = LocalArtifactStore(tmp_path / "objects")
    adapter = DomainSkillBridge(extract_type())  # type: ignore[operator]
    runtime = WorkflowRuntime(
        database=database, registry=workflows,
        skill_invoker=SkillInvoker(skills, {(skill_id, "1.0.0"): adapter}),
        skill_context=SkillContext(clock, Logger(), CancellationToken(), artifacts, {}, ResourceBudget(10)),
        artifacts=artifacts, clock=clock, identifiers=identifiers,
    )
    goal = GoalDefinition(
        "research.analyse", 1, 80, "research", NOW + timedelta(minutes=10),
        GoalBudget(100, 10, "CNY", 60),
        (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
    )
    coordinator = CognitiveCoordinator(clock, identifiers, merge_window_seconds=0)
    correlation = "00000000-0000-0000-0000-000000000999"
    coordinator.submit(EventEnvelope(
        msg_id=str(identifiers.new()), msg_type="attention.salient_event", source="research",
        occurred_at=NOW, published_at=NOW, priority=80, correlation_id=correlation,
        dedup_key="research:note:1",
        payload={"event_type": "attention.salient_event", "data": {"document_id": "note-1"}},
    ))
    formed = coordinator.form_cycle(
        WorldModel(clock).snapshot, GoalPolicy(clock, (goal,)).evaluate({"done": False}),
        MemoryContextSnapshot(0, NOW, {}), force=True,
    )
    assert formed.cycle is not None
    planner = RulePlanner(clock, identifiers, (PlanningRule(
        "research.rule.v1", "research.analyse", workflow.workflow_id, workflow.version,
        {"text": "Evidence driven agents recover safely"}, "extract research keywords",
        "research", ("attention.salient_event",),
    ),))
    policy = RiskPolicy(
        "research-policy/1", frozenset({capability_name}), frozenset(),
        RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
    )
    result = await GovernedCognitiveApp(
        database, planner, PlanValidator(workflows), RiskGate(policy),
        MotorExec(database, runtime, clock=clock, identifiers=identifiers),
        OutcomeEvaluator(database, clock, identifiers, OutcomePolicy("1.0")), clock, identifiers,
    ).execute(
        formed.cycle, BrainState(MarketPhase.CLOSED, Workload.IDLE, BrainMode.NORMAL, NOW), bindings,
    )
    assert result.execution.output["keywords"] == ["agents", "driven", "evidence", "recover", "safely"]
    assert result.outcome.successful
    trace = await TraceQuery(database).by_correlation(correlation)
    assert tuple(map(len, (trace.plans, trace.decisions, trace.grants, trace.tasks,
                               trace.workflow_runs, trace.node_runs, trace.episodes))) == (1, 1, 1, 1, 1, 1, 1)
    await database.close()


@pytest.mark.asyncio
async def test_domain_bridge_honours_cancellation_and_transaction_crash(tmp_path: Path) -> None:
    extract_type, _, _, _ = _research()
    token = CancellationToken()
    token.cancel()
    bridge = DomainSkillBridge(extract_type())  # type: ignore[operator]
    from active_agent_platform.skills import SkillBinding, SkillInvocation
    binding = SkillBinding("n", "research.text.keywords", "1.0", "research-keywords",
                           "1.0.0", "sha256:" + "1" * 64, "p", NOW)
    invocation = SkillInvocation(
        "i", "t", "r", "n", binding, {"text": "x"}, NOW + timedelta(seconds=1),
        "key", 1, frozenset(), ResourceBudget(1),
    )
    context = SkillContext(FakeClock(NOW), Logger(), token, LocalArtifactStore(tmp_path / "objects"),
                           {}, ResourceBudget(1))
    with pytest.raises(Exception, match="cancel"):  # cancellation type is part of platform contract
        await bridge.invoke(invocation, context)

    database = SQLiteDatabase(tmp_path / "crash.db")
    await database.initialize()
    with pytest.raises(RuntimeError, match="injected crash"):
        async with database.transaction() as transaction:
            await transaction.execute(
                """INSERT INTO audit_record(
                       audit_id,action,subject_type,subject_id,record_json,occurred_at,correlation_id
                   ) VALUES (?,?,?,?,?,?,?)""",
                ("research-crash", "execute", "research", "note-1", "{}", NOW.isoformat(),
                 "research-correlation"),
            )
            raise RuntimeError("injected crash")
    assert await database.fetch_one("SELECT 1 FROM audit_record WHERE audit_id='research-crash'") is None
    await database.close()
