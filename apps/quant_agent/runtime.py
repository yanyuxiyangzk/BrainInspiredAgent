"""Runnable quant composition connecting durable commands to governed execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

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
from active_agent_platform.events import (
    ConsumptionOutcome,
    DeliveryOutcome,
    OutboxRelay,
    PersistedBusMessage,
    PublishReport,
    TransactionalInboxConsumer,
)
from active_agent_platform.events.models import DeliveryResult
from active_agent_platform.foundation import SystemClock, Uuid7Generator
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
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runtime import WorkflowRuntime
from apps.quant_agent.delivery import InsightDeliveryService
from apps.quant_agent.fake_skills import install_fake_skills
from apps.quant_agent.market_summary import MARKET_SUMMARY_WORKFLOW
from apps.quant_agent.market_summary_app import MarketSummaryApp
from brain_kernel.ports import Clock, UuidGenerator


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class QuantRuntimeComponents:
    database: SQLiteDatabase
    service: QuantRuntimeService


class _CommandMessage:
    def __init__(self, message: PersistedBusMessage) -> None:
        envelope = message.envelope
        self.msg_id = message.msg_id
        self.correlation_id = str(envelope.get("correlation_id", message.msg_id))
        raw_dedup = envelope.get("dedup_key")
        self.dedup_key = None if raw_dedup is None else str(raw_dedup)
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise TypeError("command payload is invalid")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise TypeError("command data is invalid")
        self.command = str(data.get("command", ""))
        args = data.get("args", {})
        if not isinstance(args, Mapping):
            raise TypeError("command args are invalid")
        self.args = dict(args)


class _CommandPublisher:
    def __init__(self, consumer: TransactionalInboxConsumer) -> None:
        self._consumer = consumer

    async def publish(self, message: object) -> PublishReport:
        if not isinstance(message, PersistedBusMessage):
            raise TypeError("quant command publisher requires persisted messages")
        if message.msg_type != "command.received":
            return PublishReport(message.msg_id, (
                DeliveryResult("quant-command", DeliveryOutcome.FILTERED),
            ))
        command = _CommandMessage(message)

        async def accept(transaction: SQLiteTransaction, item: _CommandMessage) -> None:
            if item.command != "market.summary":
                raise ValueError("unsupported quant command")
            now = _stamp(datetime.now(UTC))
            await transaction.execute(
                """INSERT INTO command_execution(
                       command_id,dedup_key,command,args_json,status,accepted_at,correlation_id
                   ) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(dedup_key) DO NOTHING""",
                (item.msg_id, item.dedup_key or item.msg_id, item.command,
                 json.dumps(item.args, sort_keys=True, separators=(",", ":")),
                 "ACCEPTED", now, item.correlation_id),
            )

        result = await self._consumer.consume(command, accept)
        outcome = DeliveryOutcome.ENQUEUED if result.outcome in {
            ConsumptionOutcome.PROCESSED, ConsumptionOutcome.DUPLICATE
        } else DeliveryOutcome.REJECTED
        return PublishReport(message.msg_id, (DeliveryResult("quant-command", outcome),))


class QuantRuntimeService:
    name = "quant_runtime"

    def __init__(
        self,
        database: SQLiteDatabase,
        app: MarketSummaryApp,
        bindings: Mapping[tuple[str, str, str], SkillBinding],
        clock: Clock,
        identifiers: UuidGenerator,
    ) -> None:
        self._database = database
        self._app = app
        self._bindings = bindings
        self._clock = clock
        self._identifiers = identifiers
        consumer = TransactionalInboxConsumer(
            "quant-command", database, clock, identifiers,
            is_retryable=lambda error: isinstance(error, (OSError, RuntimeError)),
        )
        self._relay = OutboxRelay(
            database, _CommandPublisher(consumer), clock,
            poll_interval_seconds=0.1,
        )
        self._stopping = asyncio.Event()
        self._accepting = False

    async def start(self) -> None:
        self._stopping = asyncio.Event()
        self._accepting = True
        await self._relay.start()
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "UPDATE command_execution SET status='ACCEPTED', started_at=NULL "
                "WHERE status='RUNNING'"
            )

    async def serve(self) -> None:
        while not self._stopping.is_set():
            await self._relay.publish_due()
            if self._accepting:
                await self.process_one()
            await self._clock.sleep(0.1)

    async def process_one(self) -> bool:
        row = await self._database.fetch_one(
            "SELECT * FROM command_execution WHERE status='ACCEPTED' "
            "ORDER BY accepted_at, command_id LIMIT 1"
        )
        if row is None:
            return False
        command_id = str(row["command_id"])
        async with self._database.transaction() as transaction:
            cursor = await transaction.execute(
                "UPDATE command_execution SET status='RUNNING',started_at=?,attempt=attempt+1 "
                "WHERE command_id=? AND status='ACCEPTED'",
                (_stamp(self._clock.now()), command_id),
            )
            if cursor.rowcount != 1:
                return False
        try:
            result = await self._execute(cast(Mapping[str, Any], row))
        except Exception as error:  # noqa: BLE001 - durable command terminalizes all failures
            await self._finish(command_id, "FAILED", None, type(error).__name__)
        else:
            await self._finish(command_id, "SUCCEEDED", result, None)
        return True

    async def _execute(self, row: Mapping[str, Any]) -> Mapping[str, object]:
        args = json.loads(str(row["args_json"]))
        now = self._clock.now().astimezone(UTC)
        trade_date = args.get("trade_date") or now.date().isoformat()
        symbols = args.get("symbols") or ["INDEX.TEST"]
        title = args.get("title") or "Market summary"
        correlation = str(row["correlation_id"])
        coordinator = CognitiveCoordinator(self._clock, self._identifiers, merge_window_seconds=0)
        from active_agent_platform.events import EventEnvelope
        coordinator.submit(EventEnvelope(
            msg_id=str(self._identifiers.new()), msg_type="attention.salient_event",
            source="quant.command", occurred_at=now, published_at=now, priority=90,
            correlation_id=correlation, dedup_key=str(row["dedup_key"]),
            payload={"event_type": "attention.salient_event", "data": {"symbols": symbols}},
        ))
        goal = GoalDefinition(
            "market.summary", 1, 80, "market", now + timedelta(minutes=10),
            GoalBudget(100, 10, "CNY", 60),
            (CompletionCondition("done", "done", ConditionOperator.EQ, True),),
        )
        formed = coordinator.form_cycle(
            WorldModel(self._clock).snapshot,
            GoalPolicy(self._clock, (goal,)).evaluate({"done": False}),
            MemoryContextSnapshot(0, now, {}), force=True,
        )
        if formed.cycle is None:
            raise RuntimeError("cognitive cycle was not formed")
        planner = RulePlanner(self._clock, self._identifiers, (PlanningRule(
            "market.summary.v1", "market.summary", "market_summary", "1.0.0",
            {"symbols": list(symbols), "trade_date": str(trade_date), "title": str(title)},
            "publish deterministic market summary", "market_summary",
            ("attention.salient_event",), use_model=False,
        ),))
        executed = await self._app.execute(
            formed.cycle,
            BrainState(MarketPhase.AUCTION, Workload.IDLE, BrainMode.NORMAL, now),
            self._bindings,
            planner=planner,
        )
        if executed.execution.status.value != "SUCCEEDED":
            raise RuntimeError(f"workflow terminal status: {executed.execution.status.value}")
        insight_id = str(executed.execution.output.get("notification_id", ""))
        if insight_id:
            rows = await self._database.fetch_all(
                "SELECT subscription_id FROM insight_subscription "
                "WHERE enabled=1 AND topic='market_summary'"
            )
            delivery = InsightDeliveryService(self._database, self._clock)
            for subscription in rows:
                await delivery.deliver(str(subscription["subscription_id"]), insight_id)
        return {
            "correlation_id": correlation,
            "status": executed.execution.status.value,
            "notification_id": insight_id or None,
        }

    async def _finish(
        self, command_id: str, status: str,
        result: Mapping[str, object] | None, error_code: str | None,
    ) -> None:
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "UPDATE command_execution SET status=?,finished_at=?,result_json=?,error_code=? "
                "WHERE command_id=? AND status='RUNNING'",
                (status, _stamp(self._clock.now()),
                 None if result is None else json.dumps(result, sort_keys=True),
                 error_code, command_id),
            )

    async def quiesce(self) -> None:
        self._accepting = False
        await self._relay.quiesce()

    async def checkpoint(self) -> None:
        await self._relay.checkpoint()

    async def stop(self) -> None:
        self._accepting = False
        self._stopping.set()
        await self._relay.stop()


def build_quant_runtime(database_path: str | Path) -> QuantRuntimeComponents:
    path = Path(database_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    identifiers = Uuid7Generator(clock)
    database = SQLiteDatabase(path)
    registry = WorkflowRegistry()
    registered = registry.register(MARKET_SUMMARY_WORKFLOW, status=WorkflowStatus.VALIDATED)
    workflow = registry.activate(registered.workflow_id, registered.version)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    resolver = SkillResolver(capabilities, skills, clock=clock)
    permissions = frozenset({"market.read", "notification.local.write"})
    bindings: dict[tuple[str, str, str], SkillBinding] = {}
    nodes = cast(list[object], workflow.definition["nodes"])
    for node_value in nodes:
        if not isinstance(node_value, Mapping):
            continue
        node = dict(node_value)
        binding = resolver.resolve(SkillRequirement(
            str(node["node_id"]), str(node["capability"]),
            str(node["capability_version"]), permissions,
            SideEffect(str(dict(node["constraints"])["side_effect"])),
        ), policy_version="market-policy/1")
        bindings[(workflow.workflow_id, workflow.version, str(node["node_id"]))] = binding
    artifacts = LocalArtifactStore(path.parent / f"{path.stem}-artifacts")
    runtime = WorkflowRuntime(
        database=database, registry=registry,
        skill_invoker=SkillInvoker(skills, bundle.adapters),
        skill_context=SkillContext(
            clock, _RuntimeLogger(), CancellationToken(), artifacts, {}, ResourceBudget(10)
        ),
        artifacts=artifacts, clock=clock, identifiers=identifiers,
    )
    policy = RiskPolicy(
        "market-policy/1",
        frozenset({"market.snapshot.read", "content.summary.generate", "notification.local.send"}),
        permissions, RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
    )
    placeholder = RulePlanner(clock, identifiers, (PlanningRule(
        "market.summary.v1", "market.summary", "market_summary", "1.0.0",
        {"symbols": ["INDEX.TEST"], "trade_date": clock.now().date().isoformat(),
         "title": "Market summary"},
        "publish deterministic market summary", "market_summary",
        ("attention.salient_event",), use_model=False,
    ),))
    app = MarketSummaryApp(
        database, placeholder, PlanValidator(registry), RiskGate(policy),
        MotorExec(database, runtime, clock=clock, identifiers=identifiers),
        OutcomeEvaluator(database, clock, identifiers, OutcomePolicy("1.0")),
        clock, identifiers,
    )
    return QuantRuntimeComponents(
        database, QuantRuntimeService(database, app, bindings, clock, identifiers)
    )


class _RuntimeLogger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields
