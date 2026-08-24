"""Runnable quant composition connecting durable commands to governed execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
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
from active_agent_platform.foundation import (
    CapturingLogger,
    RuntimeDependencies,
    Settings,
    SystemClock,
    Uuid7Generator,
)
from active_agent_platform.motor import MotorExec
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.rest_repair import RestRepair
from active_agent_platform.risk import RiskBudget, RiskGate, RiskPolicy
from active_agent_platform.runtime import LoopEngine
from active_agent_platform.scheduler import MissedTriggerPolicy, Scheduler, ScheduleSpec
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
from apps.quant_agent.daily_review import DAILY_REVIEW_WORKFLOW
from apps.quant_agent.daily_review_app import DailyReviewApp
from apps.quant_agent.delivery import InsightDeliveryService
from apps.quant_agent.execution_facade import QuantExecutionFacade
from apps.quant_agent.fake_skills import (
    fake_capability_contracts,
    fake_skill_manifests,
    install_fake_skills,
)
from apps.quant_agent.market_summary import MARKET_SUMMARY_WORKFLOW
from apps.quant_agent.market_summary_app import MarketSummaryApp
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.agent_dna import (
    AgentDnaDefinition,
    AgentPolicyProfile,
    PersistentAgentDnaRegistry,
    WorkflowDnaReference,
)
from domain_sdk.dna import DnaDefinition, DnaStatus
from domain_sdk.dna_repository import PersistentDnaRegistry
from domain_sdk.organization_dna import (
    OrganizationDnaDefinition,
    OrganizationMember,
    OrganizationPolicyProfile,
    PersistentOrganizationDnaRegistry,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class QuantRuntimeComponents:
    database: SQLiteDatabase
    service: QuantRuntimeService
    engine: LoopEngine
    dna_workflows: tuple[str, ...] = ("workflow.market_summary", "workflow.daily_review")
    dna_agent: str = "agent.quant.default"
    dna_organization: str = "org.quant.default"


@dataclass(frozen=True, slots=True)
class DailyReviewSchedule:
    at: time = time(18, 0)
    timezone: str = "Asia/Shanghai"
    window_seconds: float = 60.0
    missed_policy: MissedTriggerPolicy = MissedTriggerPolicy.FIRE_ONCE
    max_missed_seconds: float = 86_400.0
    trading_days_only: bool = True


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
    def __init__(self, consumer: TransactionalInboxConsumer, service: QuantRuntimeService | None = None) -> None:
        self._consumer = consumer
        self._service = service

    async def publish(self, message: object) -> PublishReport:
        if not isinstance(message, PersistedBusMessage):
            raise TypeError("quant command publisher requires persisted messages")
        if message.msg_type == "schedule.triggered" and self._service is not None:
            await self._service.execute_daily_review(message)
            return PublishReport(message.msg_id, (
                DeliveryResult("quant-daily-review", DeliveryOutcome.ENQUEUED),
            ))
        if message.msg_type != "command.received":
            return PublishReport(message.msg_id, (
                DeliveryResult("quant-command", DeliveryOutcome.FILTERED),
            ))
        command = _CommandMessage(message)

        async def accept(transaction: SQLiteTransaction, item: _CommandMessage) -> None:
            if item.command not in {"market.summary", "task.cancel", "task.retry"}:
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
        daily_review: DailyReviewApp,
        daily_bindings: Mapping[tuple[str, str, str], SkillBinding],
        schedule: DailyReviewSchedule,
        facade: QuantExecutionFacade,
    ) -> None:
        self._database = database
        self._app = app
        self._bindings = bindings
        self._clock = clock
        self._identifiers = identifiers
        self._daily_review = daily_review
        self._daily_bindings = daily_bindings
        self._facade = facade
        consumer = TransactionalInboxConsumer(
            "quant-command", database, clock, identifiers,
            is_retryable=lambda error: isinstance(error, (OSError, RuntimeError)),
        )
        self._publisher = _CommandPublisher(consumer, self)
        self._relay = OutboxRelay(
            database, self._publisher, clock,
            poll_interval_seconds=0.1,
        )
        self._scheduler = Scheduler(database, clock, identifiers, (ScheduleSpec(
            "quant.daily_review", schedule.at, window_seconds=schedule.window_seconds,
            missed_policy=schedule.missed_policy,
            max_missed_seconds=schedule.max_missed_seconds,
            timezone=schedule.timezone, trading_days_only=schedule.trading_days_only,
            payload_data={"workflow_id": "daily_review"},
        ),), poll_interval_seconds=0.1)
        self._stopping = asyncio.Event()
        self._accepting = False

    async def start(self) -> None:
        self._stopping = asyncio.Event()
        self._accepting = True
        await self._scheduler.start()
        await self._relay.start()
        await _persist_catalog(self._database, self._clock.now())
        await _ensure_workflow_dna(self._database, self._clock, self._identifiers)
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "UPDATE command_execution SET status='ACCEPTED', started_at=NULL "
                "WHERE status='RUNNING'"
            )

    async def serve(self) -> None:
        while not self._stopping.is_set():
            await self._scheduler.tick()
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

    async def operational_snapshot(self) -> dict[str, object]:
        """Return bounded operational facts for the LoopEngine command surface."""
        commands = await self._database.fetch_one(
            "SELECT count(*) AS total FROM command_execution WHERE status IN ('ACCEPTED','RUNNING')"
        )
        outbox = await self._database.fetch_one(
            "SELECT count(*) AS total FROM outbox_event WHERE publish_state != 'PUBLISHED'"
        )
        checkpoints = await self._database.fetch_all(
            "SELECT schedule_id,occurrence_key,status,consumed_at "
            "FROM schedule_checkpoint ORDER BY occurrence_key DESC LIMIT 20"
        )
        return {
            "lag": {
                "commands": 0 if commands is None else int(commands["total"]),
                "outbox": 0 if outbox is None else int(outbox["total"]),
            },
            "checkpoints": [dict(row) for row in checkpoints],
        }

    async def _execute(self, row: Mapping[str, Any]) -> Mapping[str, object]:
        args = json.loads(str(row["args_json"]))
        command = str(row["command"])
        if command in {"task.cancel", "task.retry"}:
            return await self._execute_task_control(command, args, str(row["correlation_id"]))
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
            dna_context=await self._dna_context("market_summary"),
        )
        if executed.execution.status.value != "SUCCEEDED":
            raise RuntimeError(f"workflow terminal status: {executed.execution.status.value}")
        task_row = await self._database.fetch_one(
            "SELECT task_id FROM task WHERE grant_id=? ORDER BY created_at DESC LIMIT 1",
            (executed.grant_id,),
        )
        if task_row is not None:
            identity = await self._dna_context("market_summary")
            await self._facade.record_dna_context(self._flat_dna_context(identity) | {
                "plan_id": executed.planner.plan.plan_id,
                "decision_id": executed.decision_id, "grant_id": executed.grant_id,
                "task_id": str(task_row["task_id"]), "run_id": executed.execution.run_id,
                "episode_id": executed.outcome.episode_id,
                "evaluation_id": executed.outcome.evaluation_id,
                "correlation_id": executed.planner.plan.correlation_id,
            })
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

    async def _execute_task_control(
        self, command: str, args: Mapping[str, object], correlation_id: str,
    ) -> Mapping[str, object]:
        task_id = str(args.get("task_id", ""))
        row = await self._database.fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
        if row is None:
            raise ValueError("task does not exist")
        status = str(row["status"])
        action = command.removeprefix("task.")
        if action == "retry":
            grant = await self._database.fetch_one(
                "SELECT bindings_json FROM execution_grant WHERE grant_id=?", (row["grant_id"],)
            )
            if grant is not None and "NON_REPLAYABLE" in str(grant["bindings_json"]):
                raise ValueError("retry rejected: task binding is NON_REPLAYABLE")
        raise ValueError(
            f"{action} rejected for task in {status}: a live MotorExec control handle and "
            "new governed grant are required"
        )

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

    async def execute_daily_review(self, message: PersistedBusMessage) -> None:
        payload = message.envelope.get("payload", {})
        data = payload.get("data", {}) if isinstance(payload, Mapping) else {}
        occurrence = data.get("occurrence_key") if isinstance(data, Mapping) else None
        business_date = self._clock.now().date()
        if isinstance(occurrence, str):
            business_date = datetime.fromisoformat(occurrence).date()
        state = BrainState(
            MarketPhase.CLOSED, Workload.IDLE, BrainMode.REVIEW, self._clock.now()
        )
        result = await self._daily_review.execute(
            business_date, state, self._daily_bindings,
            dna_context=await self._dna_context("daily_review"),
        )
        if result.execution is not None and result.execution.status.value == "SUCCEEDED":
            row = await self._database.fetch_one(
                "SELECT p.plan_id,d.decision_id,g.grant_id,t.task_id,w.run_id,"
                "e.episode_id,e.evaluation_id,w.correlation_id "
                "FROM workflow_run w JOIN task t ON t.task_id=w.task_id "
                "JOIN execution_grant g ON g.grant_id=t.grant_id "
                "JOIN plan_decision d ON d.decision_id=g.decision_id "
                "JOIN plan p ON p.plan_id=d.plan_id "
                "LEFT JOIN outcome_evaluation e ON e.task_id=t.task_id "
                "WHERE w.run_id=?", (result.execution.run_id,),
            )
            if row is not None:
                await self._facade.record_dna_context(
                    self._flat_dna_context(await self._dna_context("daily_review")) | dict(row)
                )

    async def _dna_context(self, workflow_role: str) -> dict[str, object]:
        row = await self._database.fetch_one(
            "SELECT o.dna_id organization_dna_id,o.version organization_version,"
            "o.content_digest organization_content_digest,m.role organization_role,"
            "a.dna_id agent_dna_id,a.version agent_version,a.content_digest agent_content_digest,"
            "w.dna_id workflow_dna_id,w.version workflow_version,"
            "w.content_digest workflow_content_digest "
            "FROM organization_dna_definition o "
            "JOIN organization_dna_member m ON m.organization_dna_id=o.dna_id "
            "AND m.organization_version=o.version "
            "JOIN agent_dna_definition a ON a.dna_id=m.agent_dna_id "
            "AND a.version=m.agent_version "
            "JOIN agent_dna_workflow_ref r ON r.agent_dna_id=a.dna_id "
            "AND r.agent_version=a.version "
            "JOIN dna_definition w ON w.dna_id=r.workflow_dna_id "
            "AND w.version=r.workflow_version "
            "WHERE o.dna_id='org.quant.default' AND o.status='ACTIVE' "
            "AND a.status='ACTIVE' AND w.status='ACTIVE' AND m.role='lead' AND r.role=?",
            (workflow_role,),
        )
        if row is None:
            raise RuntimeError(f"active three-layer DNA is unavailable for {workflow_role}")
        identity = dict(row)
        context: dict[str, object] = {
            "organization": {"dna_id": identity["organization_dna_id"],
                             "version": identity["organization_version"],
                             "content_digest": identity["organization_content_digest"]},
            "organization_role": identity["organization_role"],
            "agent": {"dna_id": identity["agent_dna_id"], "version": identity["agent_version"],
                      "content_digest": identity["agent_content_digest"]},
            "workflow": {"dna_id": identity["workflow_dna_id"],
                         "version": identity["workflow_version"],
                         "content_digest": identity["workflow_content_digest"]},
            "responsibility": workflow_role,
        }
        digest_input = json.dumps(context, sort_keys=True, separators=(",", ":"))
        return context | {"context_digest": "sha256:" + hashlib.sha256(digest_input.encode()).hexdigest()}

    @staticmethod
    def _flat_dna_context(context: Mapping[str, object]) -> dict[str, object]:
        organization = cast(Mapping[str, object], context["organization"])
        agent = cast(Mapping[str, object], context["agent"])
        workflow = cast(Mapping[str, object], context["workflow"])
        return {
            "context_digest": context["context_digest"],
            "organization_dna_id": organization["dna_id"],
            "organization_version": organization["version"],
            "organization_content_digest": organization["content_digest"],
            "organization_role": context["organization_role"],
            "agent_dna_id": agent["dna_id"], "agent_version": agent["version"],
            "agent_content_digest": agent["content_digest"],
            "workflow_dna_id": workflow["dna_id"], "workflow_version": workflow["version"],
            "workflow_content_digest": workflow["content_digest"],
        }

    async def quiesce(self) -> None:
        self._accepting = False
        await self._scheduler.quiesce()
        await self._relay.quiesce()
        self._stopping.set()

    async def checkpoint(self) -> None:
        await self._scheduler.checkpoint()
        await self._relay.checkpoint()

    async def stop(self) -> None:
        self._accepting = False
        self._stopping.set()
        await self._scheduler.stop()
        await self._relay.stop()


def build_quant_runtime(
    database_path: str | Path, *, schedule: DailyReviewSchedule | None = None
) -> QuantRuntimeComponents:
    path = Path(database_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    identifiers = Uuid7Generator(clock)
    database = SQLiteDatabase(path)
    registry = WorkflowRegistry()
    registered = registry.register(MARKET_SUMMARY_WORKFLOW, status=WorkflowStatus.VALIDATED)
    workflow = registry.activate(registered.workflow_id, registered.version)
    daily_registered = registry.register(DAILY_REVIEW_WORKFLOW, status=WorkflowStatus.VALIDATED)
    daily_workflow = registry.activate(daily_registered.workflow_id, daily_registered.version)
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
    daily_bindings: dict[tuple[str, str, str], SkillBinding] = {}
    daily_nodes = cast(list[object], daily_workflow.definition["nodes"])
    for node_value in daily_nodes:
        if not isinstance(node_value, Mapping):
            continue
        node = dict(node_value)
        binding = resolver.resolve(SkillRequirement(
            str(node["node_id"]), str(node["capability"]),
            str(node["capability_version"]), frozenset(),
            SideEffect(str(dict(node["constraints"])["side_effect"])),
        ), policy_version="daily-review/1")
        daily_bindings[(daily_workflow.workflow_id, daily_workflow.version,
                        str(node["node_id"]))] = binding
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
    motor = MotorExec(database, runtime, clock=clock, identifiers=identifiers)
    facade = QuantExecutionFacade(motor, OutcomeEvaluator(database, clock, identifiers, OutcomePolicy("1.0")), database=database)
    app = MarketSummaryApp(
        database, placeholder, PlanValidator(registry), RiskGate(policy),
        motor,
        OutcomeEvaluator(database, clock, identifiers, OutcomePolicy("1.0")),
        clock, identifiers, facade,
    )
    daily_app = DailyReviewApp(
        database, RestRepair(database, clock, identifiers), PlanValidator(registry),
        RiskGate(RiskPolicy(
            "daily-review/1", frozenset({"content.summary.generate"}), frozenset(),
            RiskBudget(1000, 100, 600), RiskBudget(1000, 100, 600),
        )),
        motor,
        clock, identifiers, facade,
    )
    service = QuantRuntimeService(
            database, app, bindings, clock, identifiers, daily_app, daily_bindings,
            schedule or DailyReviewSchedule(), facade,
        )
    engine = LoopEngine(
        RuntimeDependencies(Settings(), clock, identifiers, CapturingLogger()),
        (service,), critical_services=frozenset({service.name}),
    )
    return QuantRuntimeComponents(database, service, engine)


class _RuntimeLogger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields


async def _persist_catalog(database: SQLiteDatabase, now: datetime) -> None:
    stamp = _stamp(now)
    workflows = (MARKET_SUMMARY_WORKFLOW, DAILY_REVIEW_WORKFLOW)
    async with database.transaction() as transaction:
        for contract in fake_capability_contracts():
            value = {
                "capability": contract.capability, "version": contract.version,
                "input_schema": dict(contract.input_schema),
                "output_schema": dict(contract.output_schema),
                "side_effect": contract.side_effect.value,
            }
            encoded, digest = _catalog_value(value)
            await transaction.execute(
                "INSERT OR IGNORE INTO capability_contract VALUES (?,?,?,?,?,?,?)",
                (contract.capability, contract.version, digest, "ACTIVE", encoded, stamp,
                 "catalog.bootstrap"),
            )
        for manifest in fake_skill_manifests():
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            await transaction.execute(
                "INSERT OR IGNORE INTO skill_manifest VALUES (?,?,?,?,?,?,?)",
                (str(manifest["skill_id"]), str(manifest["version"]), str(manifest["digest"]), "ENABLED",
                 encoded, stamp, "catalog.bootstrap"),
            )
        for workflow in workflows:
            encoded, digest = _catalog_value(workflow)
            await transaction.execute(
                "INSERT OR IGNORE INTO workflow_definition VALUES (?,?,?,?,?,?,?)",
                (str(workflow["workflow_id"]), str(workflow["version"]), digest, "ACTIVE", encoded,
                 stamp, "catalog.bootstrap"),
            )


def _catalog_value(value: Mapping[str, object]) -> tuple[str, str]:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"),
        default=lambda item: dict(item) if isinstance(item, Mapping) else str(item),
    )
    return encoded, "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


async def _ensure_workflow_dna(database: SQLiteDatabase, clock: Clock,
                               identifiers: UuidGenerator) -> None:
    """Idempotently materialize application workflows as governed DNA."""
    registry = PersistentDnaRegistry(database, clock, identifiers)
    for workflow in (MARKET_SUMMARY_WORKFLOW, DAILY_REVIEW_WORKFLOW):
        dna_id, version = f"workflow.{workflow['workflow_id']}", str(workflow["version"])
        try:
            record = await registry.get(dna_id, version)
        except (ValueError, RuntimeError):
            record = await registry.register(
                DnaDefinition.from_workflow(workflow, dna_id=dna_id, version=version,
                                            generator={"name": "bia-runtime", "version": "1"}),
                correlation_id="runtime.dna.bootstrap",
            )
        if record.dna.status is DnaStatus.CANDIDATE:
            record = await registry.transition(dna_id, version, DnaStatus.VALIDATED,
                expected_revision=record.revision, reason="runtime bootstrap validation",
                correlation_id="runtime.dna.bootstrap")
        if record.dna.status is DnaStatus.VALIDATED:
            record = await registry.transition(dna_id, version, DnaStatus.SHADOW,
                expected_revision=record.revision, reason="runtime bootstrap shadow",
                correlation_id="runtime.dna.bootstrap")
        if record.dna.status is DnaStatus.SHADOW:
            record = await registry.transition(dna_id, version, DnaStatus.CANARY,
                expected_revision=record.revision, reason="runtime bootstrap canary",
                correlation_id="runtime.dna.bootstrap")
        if record.dna.status is DnaStatus.CANARY:
            await registry.activate(dna_id, version, expected_revision=record.revision,
                reason="runtime bootstrap active", correlation_id="runtime.dna.bootstrap")
    market = await registry.get("workflow.market_summary", "1.0.0")
    daily = await registry.get("workflow.daily_review", "1.0.0")
    agents = PersistentAgentDnaRegistry(database, clock, identifiers)
    agent_profile = AgentPolicyProfile(
        goal={"allowed_goal_types": ["market.summary", "daily.review"], "max_active_goals": 3, "default_priority": 0.7},
        attention={"salience_weights": {"market_event": 1.0, "timer": 0.4}, "max_focus_items": 5, "switch_threshold": 0.6},
        planning={"strategy": "HYBRID", "horizon_seconds": 3600, "max_tasks": 8},
        memory={"working_items": 20, "episodic_retention_days": 30, "semantic_candidates": 100},
        evaluation={"minimum_evidence_score": 0.8, "minimum_value_score": 0.7, "review_interval_seconds": 86400},
    )
    agent = AgentDnaDefinition.create("agent.quant.default", "1.0.0", agent_profile, (
        WorkflowDnaReference("market_summary", market.dna.dna_id, market.dna.version, market.dna.content_digest),
        WorkflowDnaReference("daily_review", daily.dna.dna_id, daily.dna.version, daily.dna.content_digest),
    ))
    try:
        agent_record = await agents.get(agent.dna_id, agent.version)
    except ValueError:
        agent_record = await agents.register(agent, correlation_id="runtime.dna.bootstrap")
    if agent_record.dna.status is DnaStatus.CANDIDATE:
        agent_record = await agents.transition(agent.dna_id, agent.version, DnaStatus.VALIDATED, expected_revision=agent_record.revision, reason="runtime bootstrap validation", correlation_id="runtime.dna.bootstrap")
    if agent_record.dna.status is DnaStatus.VALIDATED:
        agent_record = await agents.transition(agent.dna_id, agent.version, DnaStatus.ACTIVE, expected_revision=agent_record.revision, reason="runtime bootstrap active", correlation_id="runtime.dna.bootstrap")
    organizations = PersistentOrganizationDnaRegistry(database, clock, identifiers)
    org_profile = OrganizationPolicyProfile(
        communication={"channels": ["task", "evidence"], "max_message_bytes": 65536, "max_hops": 4},
        delegation={"strategy": "RESPONSIBILITY", "max_inflight_per_agent": 2},
        arbitration={"strategy": "QUORUM", "quorum_ratio": 0.5, "tie_break_role": "lead"},
        budget={"max_tokens": 10000, "max_cost_minor": 1000, "max_duration_seconds": 3600, "max_parallel_agents": 2},
        failure={"max_member_failures": 2, "isolation_seconds": 300, "fallback_role": "lead"},
    )
    organization = OrganizationDnaDefinition.create(
        "org.quant.default", "1.0.0", org_profile,
        (OrganizationMember("lead", agent.dna_id, agent.version, agent.content_digest, ("research", "review"), 100),
         OrganizationMember("researcher", agent.dna_id, agent.version, agent.content_digest, ("research",), 80)),
    )
    try:
        org_record = await organizations.get(organization.dna_id, organization.version)
    except ValueError:
        org_record = await organizations.register(organization, correlation_id="runtime.dna.bootstrap")
    if org_record.dna.status is DnaStatus.CANDIDATE:
        org_record = await organizations.transition(organization.dna_id, organization.version, DnaStatus.VALIDATED, expected_revision=org_record.revision, reason="runtime bootstrap validation", correlation_id="runtime.dna.bootstrap")
    if org_record.dna.status is DnaStatus.VALIDATED:
        await organizations.transition(organization.dna_id, organization.version, DnaStatus.ACTIVE, expected_revision=org_record.revision, reason="runtime bootstrap active", correlation_id="runtime.dna.bootstrap")
