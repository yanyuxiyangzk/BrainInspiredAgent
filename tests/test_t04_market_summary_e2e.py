from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import (
    CognitiveCoordinator,
    CompletionCondition,
    ConditionOperator,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    MemoryContextSnapshot,
    OutcomeEvaluator,
    OutcomePolicy,
    PlanningRule,
    RulePlanner,
    WorldModel,
)
from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.motor import MotorExec
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.risk import RiskBudget, RiskGate, RiskPolicy
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from active_agent_platform.state import BrainMode, BrainState, MarketPhase, Workload
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.trace import TraceQuery
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runtime import WorkflowRuntime
from apps.quant_agent import (
    MARKET_SUMMARY_WORKFLOW,
    MarketInsightQuery,
    MarketSummaryApp,
    MarketSummaryResult,
    install_fake_skills,
)
from apps.quant_agent.cli import EXIT_NOT_FOUND, EXIT_OK
from apps.quant_agent.cli import run as run_cli

NOW = datetime(2026, 8, 18, 1, 25, tzinfo=UTC)


class Logger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields


@pytest.mark.asyncio
async def test_market_summary_runs_from_cognitive_cycle_to_trace(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    ids = FakeUuidGenerator(UUID(int=index) for index in range(1, 500))
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    registry = WorkflowRegistry()
    registered = registry.register(MARKET_SUMMARY_WORKFLOW, status=WorkflowStatus.VALIDATED)
    workflow = registry.activate(registered.workflow_id, registered.version)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    resolver = SkillResolver(capabilities, skills, clock=clock)
    permissions = frozenset({"market.read", "notification.local.write"})
    bindings = {}
    for node in workflow.definition["nodes"]:
        assert isinstance(node, Mapping)
        effect = SideEffect(str(node["constraints"]["side_effect"]))  # type: ignore[index]
        binding = resolver.resolve(SkillRequirement(
            str(node["node_id"]), str(node["capability"]),
            str(node["capability_version"]), permissions, effect,
        ), policy_version="market-policy/1")
        bindings[(workflow.workflow_id, workflow.version, str(node["node_id"]))] = binding
    artifacts = LocalArtifactStore(tmp_path / "objects")
    runtime = WorkflowRuntime(
        database=database, registry=registry, skill_invoker=SkillInvoker(skills, bundle.adapters),
        skill_context=SkillContext(
            clock, Logger(), CancellationToken(), artifacts, {}, ResourceBudget(10)
        ),
        artifacts=artifacts, clock=clock, identifiers=ids,
    )
    motor = MotorExec(database, runtime, clock=clock, identifiers=ids)
    goal = GoalDefinition(
        "market.summary", 1, 80, "market", NOW + timedelta(minutes=10),
        GoalBudget(100, 10, "CNY", 60),
        (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
    )
    coordinator = CognitiveCoordinator(clock, ids, merge_window_seconds=0)
    correlation = "00000000-0000-0000-0000-000000000999"
    coordinator.submit(EventEnvelope(
        msg_id=str(ids.new()), msg_type="attention.salient_event", source="attention",
        occurred_at=NOW, published_at=NOW, priority=90, correlation_id=correlation,
        dedup_key="2026-08-18:auction",
        payload={"event_type": "attention.salient_event", "data": {"symbol": "INDEX.TEST"}},
    ))
    formed = coordinator.form_cycle(
        WorldModel(clock).snapshot, GoalPolicy(clock, (goal,)).evaluate({"done": False}),
        MemoryContextSnapshot(0, NOW, {}), force=True,
    )
    assert formed.cycle is not None
    planner = RulePlanner(clock, ids, (PlanningRule(
        "market.summary.v1", "market.summary", "market_summary", "1.0.0",
        {"symbols": ["INDEX.TEST"], "trade_date": "2026-08-18", "title": "Auction"},
        "publish deterministic market summary", "market_summary",
        ("attention.salient_event",), use_model=True,
    ),))
    policy = RiskPolicy(
        "market-policy/1",
        frozenset({"market.snapshot.read", "content.summary.generate", "notification.local.send"}),
        permissions, RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
    )
    app = MarketSummaryApp(
        database, planner, PlanValidator(registry), RiskGate(policy), motor,
        OutcomeEvaluator(database, clock, ids, OutcomePolicy("1.0")), clock, ids,
    )
    result = await app.execute(
        formed.cycle, BrainState(MarketPhase.AUCTION, Workload.IDLE, BrainMode.NORMAL, NOW), bindings
    )
    assert isinstance(result, MarketSummaryResult)
    assert result.execution.output["delivered"] is True
    assert result.outcome.successful
    assert len(await bundle.notification.persisted_records()) == 1
    trace = await TraceQuery(database).by_correlation(correlation)
    assert tuple(map(len, (trace.plans, trace.decisions, trace.grants, trace.tasks,
                               trace.workflow_runs, trace.node_runs, trace.episodes))) == (1, 1, 1, 1, 1, 3, 1)
    query = MarketInsightQuery(database)
    latest = await query.latest(now=NOW)
    assert len(latest) == 1 and latest[0].correlation_id == correlation
    assert latest[0].evidence[0]["symbol"] == "INDEX.TEST"
    assert (await query.show(latest[0].insight_id, now=NOW)).stale is False
    explanation = await query.explain(latest[0].insight_id, now=NOW)
    assert explanation.task_id == trace.tasks[0]["task_id"]
    with pytest.raises(LookupError):
        await query.show("missing", now=NOW)
    with pytest.raises(ValueError):
        await query.latest(limit=0)
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(tmp_path / "facts.db"), "insights", "show", latest[0].insight_id),
        stdout, stderr,
    )
    assert code == EXIT_OK and json.loads(stdout.getvalue())["correlation_id"] == correlation
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(tmp_path / "facts.db"), "insights", "explain", latest[0].insight_id),
        stdout, stderr,
    )
    assert code == EXIT_OK and json.loads(stdout.getvalue())["plan_id"] == explanation.plan_id
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(tmp_path / "facts.db"), "--format", "markdown",
         "insights", "latest"), stdout, stderr,
    )
    assert code == EXIT_OK and "# Auction" in stdout.getvalue()
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(tmp_path / "facts.db"), "insights", "show", "missing"),
        stdout, stderr,
    )
    assert code == EXIT_NOT_FOUND and "NOT_FOUND" in stderr.getvalue()
    await database.close()
