"""Real WorkflowRuntime sandbox for DNA replay comparison.

The sandbox executor runs a DNA-wrapped workflow on the production
``WorkflowRuntime`` inside a disposable environment: a temporary SQLite
database, a temporary artifact store, fake virtual time stepped by a fixed
quantum, and a freshly built skill stack. Nothing touches production fact
tables, and identical ``(dna, sample, context)`` inputs always produce an
identical :class:`ReplayMeasurement` — the invariant ``DnaSandboxReplay``
relies on for its determinism checks.

Cost model: one skill invocation counts as one minor cost unit. Latency is
consumed virtual time, not wall time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.foundation import FakeClock, Uuid7Generator
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillAdapter,
    SkillBinding,
    SkillContext,
    SkillInvocation,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
    SkillResult,
)
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowRegistry, WorkflowStatus
from active_agent_platform.workflow_runs import (
    WorkflowRunRepository,
    WorkflowRunStatus,
)
from active_agent_platform.workflow_runtime import (
    WorkflowExecutionRequest,
    WorkflowExecutionResult,
    WorkflowRuntime,
)
from domain_sdk.dna import DnaDefinition
from domain_sdk.dna_replay import FaultScenario, ReplayContext, ReplayMeasurement
from domain_sdk.experience_dataset import ExperienceSample


class SandboxSkillError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxSkillLayer:
    """An isolated capability/skill stack bound to one virtual clock."""

    capabilities: CapabilityRegistry
    skills: SkillRegistry
    adapters: Mapping[tuple[str, str], SkillAdapter]


class SkillLayerProvider(Protocol):
    """Builds a fresh skill stack; called once per sandbox execution."""

    def __call__(self, clock: FakeClock) -> SandboxSkillLayer: ...


class _SilentLogger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields

    def error(self, message: str, **fields: object) -> None:
        del message, fields


class _InstrumentedInvoker:
    """Delegates to a real invoker while counting calls and injecting faults.

    The fault lands on the first skill invocation, which follows the
    workflow's deterministic topological order, so fault placement is
    reproducible across repetitions.
    """

    def __init__(
        self, inner: SkillInvoker, fault: FaultScenario, *,
        timeout_sleep_seconds: float,
    ) -> None:
        self._inner = inner
        self._fault = fault
        self._timeout_sleep_seconds = timeout_sleep_seconds
        self.invocations: tuple[str, ...] = ()

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        self.invocations += (invocation.invocation_id,)
        if len(self.invocations) == 1:
            if self._fault is FaultScenario.SKILL_FAILURE:
                return SkillResult("FAILED", {"code": "SANDBOX_FAULT_SKILL_FAILURE"})
            if self._fault is FaultScenario.CORRUPT_OUTPUT:
                return SkillResult("SUCCEEDED", {"sandbox_fault": "corrupt_output"})
            if self._fault is FaultScenario.TIMEOUT:
                await context.clock.sleep(self._timeout_sleep_seconds)  # type: ignore[attr-defined]
                return SkillResult("FAILED", {"code": "SANDBOX_FAULT_TIMEOUT"})
        return await self._inner.invoke(invocation, context)

    async def cancel(self, binding: SkillBinding, invocation_id: str) -> str:
        return await self._inner.cancel(binding, invocation_id)

    async def query_result(
        self, binding: SkillBinding, idempotency_key: str,
        provider_operation_id: str | None,
    ) -> SkillResult:
        return await self._inner.query_result(
            binding, idempotency_key, provider_operation_id,
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    quantum_seconds: float = 0.05
    max_virtual_seconds: float = 600.0
    deadline_seconds: float = 300.0
    permissions: frozenset[str] = frozenset()
    policy_version: str = "market-policy/1"

    def __post_init__(self) -> None:
        if self.quantum_seconds <= 0 or self.max_virtual_seconds <= 0:
            raise SandboxSkillError("sandbox quantum and window must be positive")
        if self.deadline_seconds <= 0:
            raise SandboxSkillError("sandbox deadline must be positive")
        if self.max_virtual_seconds < self.deadline_seconds:
            raise SandboxSkillError("sandbox window must cover the deadline")


class WorkflowSandboxExecutor:
    """Executes DNA-wrapped workflows on a real, isolated WorkflowRuntime."""

    def __init__(
        self, skill_layer: SkillLayerProvider, *, policy: SandboxPolicy | None = None,
    ) -> None:
        self._skill_layer = skill_layer
        self._policy = policy or SandboxPolicy()

    async def execute(
        self, dna: DnaDefinition, sample: ExperienceSample, context: ReplayContext,
    ) -> ReplayMeasurement:
        with tempfile.TemporaryDirectory(prefix="dna-sandbox-") as directory:
            root = Path(directory)
            database = SQLiteDatabase(root / "sandbox.db")
            await database.initialize()
            try:
                # 沙箱是一次性度量库：关闭外键以跳过治理事实链（task→grant→
                # decision→plan）。沙箱只产生重放度量，不伪造治理事实。
                await database.fetch_one("PRAGMA foreign_keys=OFF")
                return await self._execute(database, root, dna, sample, context)
            finally:
                with contextlib.suppress(Exception):
                    await database.close()

    async def _execute(
        self, database: SQLiteDatabase, root: Path, dna: DnaDefinition,
        sample: ExperienceSample, context: ReplayContext,
    ) -> ReplayMeasurement:
        policy = self._policy
        clock = SandboxClock(context.virtual_time)
        layer = self._skill_layer(clock)
        registry = WorkflowRegistry()
        # dna.workflow 是冻结形式（tuple 节点）；Registry 校验要求原始
        # dict/list 形态并自行冻结，因此通过 to_document 取纯化副本。
        workflow_document = cast(
            "Mapping[str, object]", dna.to_document()["workflow"],
        )
        registered = registry.register(workflow_document, status=WorkflowStatus.VALIDATED)
        workflow = registry.activate(registered.workflow_id, registered.version)
        bindings = _resolve_bindings(layer, workflow.definition, policy, clock)

        invoker = _InstrumentedInvoker(
            SkillInvoker(layer.skills, layer.adapters),
            context.fault, timeout_sleep_seconds=policy.max_virtual_seconds,
        )
        cancellation = CancellationToken()
        if context.fault is FaultScenario.CANCELLED:
            cancellation.cancel()
        artifacts = LocalArtifactStore(root / "artifacts")
        skill_context = SkillContext(
            clock, _SilentLogger(), cancellation, artifacts, {}, ResourceBudget(100),
        )
        runtime = WorkflowRuntime(
            database=database, registry=registry,
            skill_invoker=cast("SkillInvoker", invoker),
            skill_context=skill_context, artifacts=artifacts, clock=clock,
            identifiers=Uuid7Generator(clock),
        )
        request = WorkflowExecutionRequest(
            run_id="sandbox-run", task_id="sandbox-task", workflow=workflow,
            parameters=_parameters(sample), bindings=bindings,
            deadline=context.virtual_time + timedelta(seconds=policy.deadline_seconds),
            correlation_id=context.replay_id,
            allowed_permissions=policy.permissions,
            cancellation=cancellation,
        )
        # WorkflowRuntime.execute 假设调用方（MotorExec）已创建 run 行；
        # 子流程路径同样先 create_workflow 再 execute。
        async with database.transaction() as transaction:
            await WorkflowRunRepository(transaction).create_workflow(
                run_id=request.run_id, task_id=request.task_id,
                workflow_id=workflow.workflow_id, workflow_version=workflow.version,
                workflow_digest=workflow.digest,
                input_digest=_output_digest(request.parameters),
                deadline=request.deadline, created_at=clock.now(),
                correlation_id=request.correlation_id,
                transition_id=str(Uuid7Generator(clock).new()),
                event_id=str(Uuid7Generator(clock).new()),
            )
        try:
            result = await _drive(runtime, request, clock, policy)
        except _VirtualDeadlineExceeded:
            return _measurement_from_failure(
                invoker.invocations, policy, "virtual_deadline_exceeded",
            )
        violations: list[str] = []
        if result.status is WorkflowRunStatus.FAILED and result.error_id is None:
            violations.append("uncontrolled_failure")
        if (result.status is WorkflowRunStatus.CANCELLED
                and context.fault is not FaultScenario.CANCELLED):
            violations.append("cancelled_without_fault")
        successful = result.status is WorkflowRunStatus.SUCCEEDED
        return ReplayMeasurement(
            successful=successful,
            evidence_score=_evidence_score(successful, result.output),
            user_value_score=1.0 if successful else 0.0,
            cost_minor=len(invoker.invocations),
            latency_ms=await _virtual_latency_ms(database, request.run_id),
            stable=successful and not violations,
            risk_violations=tuple(violations),
            output_digest=_output_digest(result.output),
        )


class _VirtualDeadlineExceeded(Exception):
    pass


class SandboxClock(FakeClock):
    """FakeClock exposing the earliest pending sleeper for deterministic stepping."""

    def next_sleep_deadline(self) -> float | None:
        return self._sleepers[0][0] if self._sleepers else None


async def _drive(
    runtime: WorkflowRuntime, request: WorkflowExecutionRequest,
    clock: SandboxClock, policy: SandboxPolicy,
) -> WorkflowExecutionResult:
    """Run the workflow, advancing virtual time only at explicit sleeps.

    Determinism rule: virtual time moves exclusively when the workflow
    itself awaits ``clock.sleep`` (delay nodes, retry backoff, injected
    timeouts). Waiting on database commits yields the loop without
    consuming virtual time, so node timestamps never depend on real thread
    interleaving.
    """
    task: asyncio.Task[WorkflowExecutionResult] = asyncio.ensure_future(
        runtime.execute(request),
    )
    yield_rounds = 0
    max_yield_rounds = int(policy.max_virtual_seconds * 200)  # 5 ms per round
    while not task.done():
        deadline = clock.next_sleep_deadline()
        if deadline is not None:
            clock.advance(max(0.0, deadline - clock.monotonic()))
            yield_rounds = 0
        else:
            await asyncio.sleep(0.005)
            yield_rounds += 1
            if yield_rounds >= max_yield_rounds:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise _VirtualDeadlineExceeded
    return await task


async def _virtual_latency_ms(database: SQLiteDatabase, run_id: str) -> int:
    """Latency as the virtual span from first node start to last node finish.

    Node timestamps are stamped from the sandbox's virtual clock, so this
    span is fully deterministic even though real thread interleaving varies.
    """
    rows = await database.fetch_all(
        "SELECT started_at, finished_at FROM node_run WHERE run_id=?",
        (run_id,),
    )
    stamps = [datetime.fromisoformat(str(row[field]))
              for row in rows for field in ("started_at", "finished_at")
              if row[field] is not None]
    if not stamps:
        return 0
    return round((max(stamps) - min(stamps)).total_seconds() * 1000)


def _resolve_bindings(
    layer: SandboxSkillLayer, definition: Mapping[str, object],
    policy: SandboxPolicy, clock: FakeClock,
) -> dict[tuple[str, str, str], SkillBinding]:
    """Pin one fixed binding per skill node, mirroring production wiring."""
    resolver = SkillResolver(layer.capabilities, layer.skills, clock=clock)
    bindings: dict[tuple[str, str, str], SkillBinding] = {}
    for node_value in cast("list[object]", definition["nodes"]):
        if not isinstance(node_value, Mapping):
            continue
        node = cast("Mapping[str, object]", node_value)
        node_id = str(node["node_id"])
        constraints = cast("Mapping[str, object]", node["constraints"])
        bindings[(str(definition["workflow_id"]), str(definition["version"]), node_id)] = (
            resolver.resolve(SkillRequirement(
                node_id, str(node["capability"]), str(node["capability_version"]),
                policy.permissions, SideEffect(str(constraints["side_effect"])),
            ), policy_version=policy.policy_version)
        )
    return bindings


def _parameters(sample: ExperienceSample) -> Mapping[str, object]:
    document = sample.document
    parameters = document.get("parameters")
    if isinstance(parameters, Mapping):
        return cast("Mapping[str, object]", parameters)
    return document


def _evidence_score(successful: bool, output: Mapping[str, object]) -> float:
    if not successful:
        return 0.0
    if any(_non_empty(value) for value in output.values()):
        return 1.0
    return 0.5


def _non_empty(value: object) -> bool:
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, (list, tuple, str)):
        return len(value) > 0
    return value is not None


def _measurement_from_failure(
    invocations: tuple[str, ...], policy: SandboxPolicy, reason: str,
) -> ReplayMeasurement:
    return ReplayMeasurement(
        successful=False, evidence_score=0.0, user_value_score=0.0,
        cost_minor=len(invocations),
        latency_ms=round(policy.max_virtual_seconds * 1000),
        stable=False, risk_violations=(reason,),
        output_digest="sha256:" + hashlib.sha256(
            b"sandbox-fault:" + reason.encode(),
        ).hexdigest(),
    )


def _output_digest(output: Mapping[str, object]) -> str:
    canonical = json.dumps(_plain(output), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
