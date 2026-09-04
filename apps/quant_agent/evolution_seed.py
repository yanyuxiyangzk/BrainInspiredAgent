"""E00 cold start: seed baseline DNA fitness from governed market summaries.

Drives the real governed chain (Coordinator → Planner → Validator → RiskGate
→ MotorExec → OutcomeEvaluator) once per virtual day on the deterministic
fake market skills, then attributes every outcome to the ACTIVE baseline
workflow DNA through the fitness projector. The result is a cold-started
fitness history that replay-based evolution (E01+) can compare against.

Deterministic by construction: a frozen start time, a manual clock and a
zero-entropy id generator reproduce identical facts on every run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from active_agent_platform import (
    CognitiveCoordinator,
    CompletionCondition,
    ConditionOperator,
    GoalBudget,
    GoalDefinition,
    GoalPolicy,
    MemoryContextSnapshot,
    OutcomeEvaluation,
    OutcomeEvaluator,
    OutcomePolicy,
    PlanningRule,
    RulePlanner,
    WorldModel,
)
from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.events import EventEnvelope
from active_agent_platform.foundation import FakeClock, Uuid7Generator
from active_agent_platform.motor import MotorExec
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.risk import RiskBudget, RiskGate, RiskPolicy
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillBinding,
    SkillContext,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from active_agent_platform.state import BrainMode, BrainState, MarketPhase, Workload
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowDefinition, WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runtime import WorkflowRuntime
from apps.quant_agent import MARKET_SUMMARY_WORKFLOW, MarketSummaryApp, install_fake_skills
from apps.quant_agent.runtime import _ensure_workflow_dna
from domain_sdk.dna_fitness import (
    DnaFitnessObservation,
    DnaFitnessPolicy,
    DnaFitnessProjector,
    DnaFitnessSnapshot,
)

BASELINE_DNA_ID = "workflow.market_summary"
DEFAULT_START = datetime(2026, 1, 5, 1, 25, tzinfo=UTC)


class SeedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeedDay:
    trade_date: str
    evaluation_id: str
    correlation_id: str
    successful: bool


@dataclass(frozen=True, slots=True)
class SeedReport:
    dna_id: str
    version: str
    content_digest: str
    window_id: str
    days: tuple[SeedDay, ...]
    observation_count: int
    readiness: str

    def to_dict(self) -> dict[str, object]:
        return {
            "dna_id": self.dna_id, "version": self.version,
            "content_digest": self.content_digest, "window_id": self.window_id,
            "days": [vars(day) for day in self.days],
            "observation_count": self.observation_count, "readiness": self.readiness,
        }


async def seed_baseline(
    database: SQLiteDatabase, *, start: datetime = DEFAULT_START, days: int = 5,
    symbols: tuple[str, ...] = ("INDEX.TEST",), artifacts_dir: Path | None = None,
) -> SeedReport:
    """Run one governed market summary per virtual day and project fitness."""
    if days < 1:
        raise SeedError("seed requires at least one day")
    if start.tzinfo is None or start.utcoffset() is None:
        raise SeedError("seed start must be timezone-aware")
    clock = FakeClock(start)
    identifiers = Uuid7Generator(clock, random_bits=lambda bits: 0)
    await _ensure_workflow_dna(database, clock, identifiers)
    baseline = await _active_baseline(database)

    registry = WorkflowRegistry()
    registered = registry.register(MARKET_SUMMARY_WORKFLOW, status=WorkflowStatus.VALIDATED)
    workflow = registry.activate(registered.workflow_id, registered.version)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    resolver = SkillResolver(capabilities, skills, clock=clock)
    permissions = frozenset({"market.read", "notification.local.write"})
    bindings: dict[tuple[str, str, str], SkillBinding] = {}
    nodes = cast("tuple[object, ...]", workflow.definition["nodes"])
    for node_value in nodes:
        if not isinstance(node_value, Mapping):
            continue
        node = cast("Mapping[str, object]", node_value)
        constraints = cast("Mapping[str, object]", node["constraints"])
        effect = SideEffect(str(constraints["side_effect"]))
        bindings[(workflow.workflow_id, workflow.version, str(node["node_id"]))] = (
            resolver.resolve(SkillRequirement(
                str(node["node_id"]), str(node["capability"]),
                str(node["capability_version"]), permissions, effect,
            ), policy_version="market-policy/1")
        )
    artifacts = LocalArtifactStore(artifacts_dir or Path(tempfile.mkdtemp(prefix="seed-art-")))
    runtime = WorkflowRuntime(
        database=database, registry=registry,
        skill_invoker=SkillInvoker(skills, bundle.adapters),
        skill_context=SkillContext(
            clock, _SilentLogger(), CancellationToken(), artifacts, {}, ResourceBudget(100),
        ),
        artifacts=artifacts, clock=clock, identifiers=identifiers,
    )
    motor = MotorExec(database, runtime, clock=clock, identifiers=identifiers)
    risk_policy = RiskPolicy(
        "market-policy/1",
        frozenset({"market.snapshot.read", "content.summary.generate", "notification.local.send"}),
        permissions, RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
    )
    evaluator = OutcomeEvaluator(database, clock, identifiers, OutcomePolicy("1.0"))
    window_id = f"seed-{start:%Y%m%d}-{days}"
    projector = DnaFitnessProjector(database, clock, identifiers, DnaFitnessPolicy(
        "seed/1.0", window_id, start, start + timedelta(days=days + 1),
        minimum_samples=days,
    ))

    seeded: list[SeedDay] = []
    day_seconds = timedelta(days=1).total_seconds()
    for offset in range(days):
        clock.advance(offset * day_seconds)
        trade_date = (start + timedelta(days=offset)).date().isoformat()
        correlation = f"00000000-0000-0000-0000-{offset:012d}"
        outcome = await _run_day(
            database, clock, identifiers, registry, workflow, motor, evaluator,
            risk_policy, trade_date, correlation, tuple(symbols), bindings,
        )
        observation = await _observation(database, outcome, baseline)
        await projector.project(observation)
        seeded.append(SeedDay(trade_date, outcome.evaluation_id, correlation,
                              outcome.successful))

    snapshot: DnaFitnessSnapshot = await projector.get(baseline["dna_id"], baseline["version"])
    return SeedReport(
        baseline["dna_id"], baseline["version"], baseline["content_digest"], window_id,
        tuple(seeded), snapshot.sample_count, snapshot.readiness.value,
    )


async def _run_day(
    database: SQLiteDatabase, clock: FakeClock, identifiers: Uuid7Generator,
    registry: WorkflowRegistry, workflow: WorkflowDefinition,
    motor: MotorExec, evaluator: OutcomeEvaluator, risk_policy: RiskPolicy,
    trade_date: str, correlation: str, symbols: tuple[str, ...],
    bindings: Mapping[tuple[str, str, str], SkillBinding],
) -> OutcomeEvaluation:
    now = clock.now()
    goal = GoalDefinition(
        "market.summary", 1, 80, "market", now + timedelta(minutes=10),
        GoalBudget(100, 10, "CNY", 60),
        (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
    )
    coordinator = CognitiveCoordinator(clock, identifiers, merge_window_seconds=0)
    coordinator.submit(EventEnvelope(
        msg_id=str(identifiers.new()), msg_type="attention.salient_event", source="attention",
        occurred_at=now, published_at=now, priority=90, correlation_id=correlation,
        dedup_key=f"{trade_date}:seed",
        payload={"event_type": "attention.salient_event",
                 "data": {"symbol": symbols[0], "trade_date": trade_date}},
    ))
    formed = coordinator.form_cycle(
        WorldModel(clock).snapshot, GoalPolicy(clock, (goal,)).evaluate({"done": False}),
        MemoryContextSnapshot(0, now, {}), force=True,
    )
    if formed.cycle is None:
        raise SeedError(f"seed day {trade_date} formed no cognitive cycle")
    planner = RulePlanner(clock, identifiers, (PlanningRule(
        "market.summary.v1", "market.summary", "market_summary", workflow.version,
        {"symbols": list(symbols), "trade_date": trade_date, "title": "Market summary"},
        "publish deterministic market summary", workflow.workflow_id,
        ("attention.salient_event",), use_model=False,
    ),))
    app = MarketSummaryApp(
        database, planner, PlanValidator(registry), RiskGate(risk_policy),
        motor, evaluator, clock, identifiers,
    )
    result = await app.execute(
        formed.cycle,
        BrainState(MarketPhase.AUCTION, Workload.IDLE, BrainMode.NORMAL, now),
        bindings,
    )
    return result.outcome


async def _observation(
    database: SQLiteDatabase, outcome: OutcomeEvaluation, baseline: Mapping[str, str],
) -> DnaFitnessObservation:
    run = await database.fetch_one(
        "SELECT run_id FROM workflow_run WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (outcome.task_id,),
    )
    if run is None:
        raise SeedError(f"outcome {outcome.evaluation_id} has no workflow run")
    cost_row = await database.fetch_one(
        "SELECT COUNT(*) AS cost FROM node_run "
        "WHERE run_id=? AND skill_binding_id IS NOT NULL",
        (str(run["run_id"]),),
    )
    return DnaFitnessObservation(
        baseline["dna_id"], baseline["version"], baseline["content_digest"], outcome,
        cost_minor=int(cost_row["cost"]) if cost_row else 0,
        latency_ms=0, stable=outcome.successful, risk_violations=(),
    )


async def _active_baseline(database: SQLiteDatabase) -> dict[str, str]:
    row = await database.fetch_one(
        "SELECT dna_id,version,content_digest FROM dna_definition "
        "WHERE dna_id=? AND status='ACTIVE'",
        (BASELINE_DNA_ID,),
    )
    if row is None:
        raise SeedError("baseline market summary DNA is not registered as ACTIVE")
    return {"dna_id": str(row["dna_id"]), "version": str(row["version"]),
            "content_digest": str(row["content_digest"])}


class _SilentLogger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields

    def error(self, message: str, **fields: object) -> None:
        del message, fields


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed baseline DNA fitness (E00)")
    parser.add_argument("--database", required=True)
    parser.add_argument("--days", type=int, default=5)
    arguments = parser.parse_args()
    database = SQLiteDatabase(Path(arguments.database))
    await database.initialize()
    try:
        report = await seed_baseline(database, days=arguments.days)
    finally:
        await database.close()
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
