"""Deterministic execution of the five Workflow 1.0 node types."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import cast

from active_agent_platform.artifacts import LocalArtifactStore
from active_agent_platform.skills import (
    CancellationToken,
    SkillBinding,
    SkillContext,
    SkillInvocation,
    SkillInvoker,
)
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import (
    WorkflowDefinition,
    WorkflowRegistry,
    evaluate_expression,
    resolve_json_path,
)
from active_agent_platform.workflow_runs import (
    NODE_TERMINAL,
    NodeRun,
    NodeRunStatus,
    WorkflowRunRepository,
    WorkflowRunStatus,
)
from brain_kernel.ports import Clock, UuidGenerator


class WorkflowExecutionError(RuntimeError):
    pass


class _WorkflowCancelled(Exception):
    pass


class _WorkflowTimedOut(Exception):
    pass


class _NodeTimedOut(Exception):
    pass


class _NodeFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkflowExecutionRequest:
    run_id: str
    task_id: str
    workflow: WorkflowDefinition
    parameters: Mapping[str, object]
    bindings: Mapping[tuple[str, str, str], SkillBinding]
    deadline: datetime
    correlation_id: str
    allowed_permissions: frozenset[str] = frozenset()
    depth: int = 0
    cancellation: CancellationToken | None = None


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    run_id: str
    status: WorkflowRunStatus
    output: Mapping[str, object]
    node_statuses: Mapping[str, NodeRunStatus]
    error_id: str | None = None


@dataclass(slots=True)
class _ExecutionState:
    request: WorkflowExecutionRequest
    nodes: Mapping[str, Mapping[str, object]]
    outputs: dict[str, object]
    statuses: dict[str, NodeRunStatus]
    executed: set[str]
    tolerated_failures: set[str]
    attempts: dict[str, int] = field(default_factory=dict)
    active_invocations: dict[str, tuple[SkillBinding, str, CancellationToken]] = field(
        default_factory=dict
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def context(self) -> dict[str, object]:
        return {
            "params": dict(self.request.parameters),
            "nodes": {
                node_id: {"output": self.outputs.get(node_id), "status": status.value}
                for node_id, status in self.statuses.items()
            },
            "context": {"correlation_id": self.request.correlation_id},
        }


class WorkflowRuntime:
    """Runs validated workflows with bounded concurrency, deadlines and cancellation."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        registry: WorkflowRegistry,
        skill_invoker: SkillInvoker,
        skill_context: SkillContext,
        artifacts: LocalArtifactStore,
        clock: Clock,
        identifiers: UuidGenerator,
        max_depth: int = 8,
        global_parallelism: int = 16,
        skill_parallelism: int = 4,
    ) -> None:
        if min(max_depth, global_parallelism, skill_parallelism) < 1:
            raise ValueError("depth and parallelism limits must be positive")
        self._database = database
        self._registry = registry
        self._skill_invoker = skill_invoker
        self._skill_context = skill_context
        self._artifacts = artifacts
        self._clock = clock
        self._identifiers = identifiers
        self._max_depth = max_depth
        self._global_limit = asyncio.Semaphore(global_parallelism)
        self._default_skill_parallelism = skill_parallelism
        self._skill_limits: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._workflow_limits: dict[str, asyncio.Semaphore] = {}
        self._states: dict[str, _ExecutionState] = {}

    async def cancel(self, run_id: str) -> bool:
        """Request cancellation and best-effort cancel every active adapter invocation."""
        state = self._states.get(run_id)
        if state is None:
            return False
        cancellation = state.request.cancellation
        if cancellation is not None:
            cancellation.cancel()
        calls = []
        related = (
            candidate
            for candidate in tuple(self._states.values())
            if candidate.request.cancellation is cancellation
        )
        for candidate in related:
            for binding, invocation_id, token in tuple(candidate.active_invocations.values()):
                token.cancel()
                calls.append(self._skill_invoker.cancel(binding, invocation_id))
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        return True

    async def execute(self, request: WorkflowExecutionRequest) -> WorkflowExecutionResult:
        if request.depth >= self._max_depth:
            raise WorkflowExecutionError("maximum sub-workflow depth exceeded")
        if request.deadline.tzinfo is None or request.deadline.utcoffset() is None:
            raise WorkflowExecutionError("workflow deadline must be timezone-aware")
        definition = request.workflow.definition
        raw_nodes = definition["nodes"]
        if not isinstance(raw_nodes, tuple):
            raise WorkflowExecutionError("workflow definition is not frozen and validated")
        nodes = {
            str(node["node_id"]): cast(Mapping[str, object], node)
            for node in raw_nodes
            if isinstance(node, Mapping)
        }
        cancellation = request.cancellation or CancellationToken()
        request = replace(request, cancellation=cancellation)
        state = _ExecutionState(request, nodes, {}, {}, set(), set())
        policy = cast(Mapping[str, object], definition["policy"])
        self._workflow_limits[request.run_id] = asyncio.Semaphore(
            cast(int, policy["max_parallelism"])
        )
        self._states[request.run_id] = state
        await self._start_workflow(state)
        compensation_nodes = {
            str(node["compensation_node"])
            for node in nodes.values()
            if node.get("failure_policy") == "compensate"
        }
        try:
            for node_id in request.workflow.validation.topological_order:
                self._guard(state)
                if node_id not in state.executed and node_id not in compensation_nodes:
                    await self._execute_node(state, node_id)
            for node_id in compensation_nodes - state.executed:
                await self._skip_node(state, node_id, "compensation was not required")
            self._guard(state)
        except _WorkflowCancelled:
            await self._cancel_remaining(state, "workflow cancellation requested")
            await self._finish_workflow(
                request.run_id, WorkflowRunStatus.CANCELLED, "workflow cancelled", None
            )
            return WorkflowExecutionResult(
                request.run_id, WorkflowRunStatus.CANCELLED, {}, dict(state.statuses)
            )
        except _WorkflowTimedOut:
            await self._cancel_remaining(state, "workflow deadline exceeded")
            error_id = self._new_id()
            await self._finish_workflow(
                request.run_id, WorkflowRunStatus.TIMED_OUT, "workflow deadline exceeded", error_id
            )
            return WorkflowExecutionResult(
                request.run_id, WorkflowRunStatus.TIMED_OUT, {}, dict(state.statuses), error_id
            )
        finally:
            self._states.pop(request.run_id, None)
            self._workflow_limits.pop(request.run_id, None)
        failures = [
            node_id
            for node_id, status in state.statuses.items()
            if status in {NodeRunStatus.FAILED, NodeRunStatus.TIMED_OUT, NodeRunStatus.REQUIRES_REVIEW}
            and node_id not in state.tolerated_failures
        ]
        if failures:
            error_id = self._new_id()
            await self._finish_workflow(
                request.run_id,
                WorkflowRunStatus.FAILED,
                "one or more nodes failed",
                error_id,
            )
            return WorkflowExecutionResult(
                request.run_id,
                WorkflowRunStatus.FAILED,
                {},
                dict(state.statuses),
                error_id,
            )
        output = cast(dict[str, object], _resolve_value(definition["output_mapping"], state.context()))
        await self._finish_workflow(
            request.run_id, WorkflowRunStatus.SUCCEEDED, "workflow completed", None
        )
        return WorkflowExecutionResult(
            request.run_id,
            WorkflowRunStatus.SUCCEEDED,
            output,
            dict(state.statuses),
        )

    async def _start_workflow(self, state: _ExecutionState) -> None:
        request = state.request
        async with self._database.transaction() as transaction:
            repository = WorkflowRunRepository(transaction)
            workflow = await repository.get_workflow(request.run_id)
            if workflow.status is WorkflowRunStatus.PENDING:
                workflow = await repository.transition_workflow(
                    request.run_id,
                    WorkflowRunStatus.READY,
                    expected_version=workflow.version,
                    reason="workflow admitted",
                    occurred_at=self._clock.now(),
                    transition_id=self._new_id(),
                    event_id=self._new_id(),
                )
            if workflow.status is WorkflowRunStatus.READY:
                await repository.transition_workflow(
                    request.run_id,
                    WorkflowRunStatus.RUNNING,
                    expected_version=workflow.version,
                    reason="workflow execution started",
                    occurred_at=self._clock.now(),
                    transition_id=self._new_id(),
                    event_id=self._new_id(),
                )
            for node_id in state.nodes:
                binding = request.bindings.get(
                    (request.workflow.workflow_id, request.workflow.version, node_id)
                )
                created = await repository.create_node(
                    run_id=request.run_id,
                    node_id=node_id,
                    attempt=1,
                    created_at=self._clock.now(),
                    correlation_id=request.correlation_id,
                    transition_id=self._new_id(),
                    event_id=self._new_id(),
                    skill_binding_id=None if binding is None else binding.skill_id,
                )
                state.statuses[node_id] = created.status
                state.attempts[node_id] = 1

    async def _create_attempt(self, state: _ExecutionState, node_id: str, attempt: int) -> None:
        request = state.request
        binding = request.bindings.get(
            (request.workflow.workflow_id, request.workflow.version, node_id)
        )
        async with self._database.transaction() as transaction:
            await WorkflowRunRepository(transaction).create_node(
                run_id=request.run_id,
                node_id=node_id,
                attempt=attempt,
                created_at=self._clock.now(),
                correlation_id=request.correlation_id,
                transition_id=self._new_id(),
                event_id=self._new_id(),
                skill_binding_id=None if binding is None else binding.skill_id,
            )

    async def _execute_node(
        self, state: _ExecutionState, node_id: str, *, active_controller: str | None = None
    ) -> NodeRunStatus:
        async with state.lock:
            if node_id in state.executed:
                return state.statuses[node_id]
        node = state.nodes[node_id]
        dependencies = cast(tuple[str, ...], node["depends_on"])
        blocking = {
            NodeRunStatus.FAILED,
            NodeRunStatus.TIMED_OUT,
            NodeRunStatus.CANCELLED,
            NodeRunStatus.REQUIRES_REVIEW,
        }
        for dependency in dependencies:
            status = state.statuses[dependency]
            if dependency == active_controller and status is NodeRunStatus.RUNNING:
                continue
            if status in blocking:
                await self._skip_node(state, node_id, "dependency did not succeed")
                return NodeRunStatus.SKIPPED
            if status not in {NodeRunStatus.SUCCEEDED, NodeRunStatus.SKIPPED}:
                raise WorkflowExecutionError(f"dependency is not terminal: {dependency}")
        retry = cast(Mapping[str, object], node.get("retry", {}))
        max_attempts = cast(int, retry.get("max_attempts", 1))
        retry_on = set(cast(tuple[str, ...], retry.get("retry_on", ())))
        backoff = float(cast(int | float, retry.get("backoff_seconds", 0)))
        for attempt in range(1, max_attempts + 1):
            self._guard(state)
            if attempt > 1:
                await self._create_attempt(state, node_id, attempt)
            state.attempts[node_id] = attempt
            await self._transition_node(state, node_id, NodeRunStatus.READY, "dependencies satisfied")
            await self._transition_node(state, node_id, NodeRunStatus.RUNNING, "node execution started")
            try:
                output, succeeded = await self._dispatch_with_limits(state, node_id, node)
                self._guard(state)
                if not succeeded:
                    raise _NodeFailure("NODE_RETURNED_FAILURE")
            except asyncio.CancelledError:
                await self._transition_node(
                    state, node_id, NodeRunStatus.CANCELLED, "parallel branch cancelled"
                )
                raise
            except _WorkflowCancelled:
                await self._transition_node(
                    state, node_id, NodeRunStatus.CANCELLED, "workflow cancellation requested"
                )
                raise
            except _WorkflowTimedOut:
                await self._timeout_node(state, node_id, "node deadline exceeded")
                raise _WorkflowTimedOut
            except _NodeTimedOut:
                await self._timeout_node(state, node_id, "node timeout exceeded")
                return NodeRunStatus.TIMED_OUT
            except Exception as error:  # noqa: BLE001 - adapter boundary
                code = getattr(error, "code", None) or (
                    error.code if isinstance(error, _NodeFailure) else type(error).__name__
                )
                await self._fail_node(state, node_id, f"node failed: {code}")
                if attempt < max_attempts and code in retry_on:
                    if backoff:
                        await self._clock.sleep(backoff)
                    continue
                return NodeRunStatus.FAILED
            state.outputs[node_id] = output
            await self._complete_node(state, node_id, output)
            return NodeRunStatus.SUCCEEDED
        return NodeRunStatus.FAILED

    async def _dispatch_with_limits(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        remaining = self._remaining(state)
        timeout = remaining
        node_limited = False
        if "timeout_seconds" in node:
            node_timeout = float(cast(int, node["timeout_seconds"]))
            node_limited = node_timeout < remaining
            timeout = min(timeout, node_timeout)
        if timeout <= 0:
            raise _WorkflowTimedOut
        workflow_limit = self._workflow_limits[state.request.run_id]
        skill_limit: asyncio.Semaphore | None = None
        if node["type"] == "skill":
            binding = state.request.bindings.get(
                (state.request.workflow.workflow_id, state.request.workflow.version, node_id)
            )
            if binding is not None:
                key = (binding.skill_id, binding.skill_version)
                skill_limit = self._skill_limits.setdefault(
                    key, asyncio.Semaphore(self._default_skill_parallelism)
                )

        async def bounded() -> tuple[object, bool]:
            if node["type"] != "skill":
                return await self._dispatch(state, node_id, node)
            async with self._global_limit, workflow_limit:
                if skill_limit is None:
                    return await self._dispatch(state, node_id, node)
                async with skill_limit:
                    return await self._dispatch(state, node_id, node)

        try:
            return await asyncio.wait_for(bounded(), timeout=timeout)
        except TimeoutError as error:
            if node_limited:
                raise _NodeTimedOut from error
            raise _WorkflowTimedOut from error

    async def _dispatch(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        node_type = str(node["type"])
        if node_type == "skill":
            return await self._run_skill(state, node_id, node)
        if node_type == "condition":
            return await self._run_condition(state, node_id, node)
        if node_type == "parallel":
            return await self._run_parallel(state, node_id, node)
        if node_type == "delay":
            return await self._run_delay(node)
        if node_type == "sub_workflow":
            return await self._run_sub_workflow(state, node_id, node)
        raise WorkflowExecutionError(f"unsupported node type: {node_type}")

    async def _run_skill(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        request = state.request
        key = (request.workflow.workflow_id, request.workflow.version, node_id)
        binding = request.bindings.get(key)
        if binding is None:
            raise WorkflowExecutionError(f"missing fixed SkillBinding for {node_id}")
        input_value = _resolve_value(node["input"], state.context())
        if not isinstance(input_value, Mapping):
            raise WorkflowExecutionError("skill input must resolve to an object")
        attempt = state.attempts[node_id]
        invocation_id = self._new_id()
        invocation = SkillInvocation(
            invocation_id,
            request.task_id,
            request.run_id,
            node_id,
            binding,
            input_value,
            request.deadline,
            f"{request.task_id}:{request.run_id}:{node_id}",
            attempt,
            request.allowed_permissions,
            self._skill_context.budget,
        )
        token = CancellationToken()
        context = replace(self._skill_context, cancellation=token)
        state.active_invocations[node_id] = (binding, invocation_id, token)
        try:
            result = await self._skill_invoker.invoke(invocation, context)
            if request.cancellation is not None and request.cancellation.cancelled:
                raise _WorkflowCancelled
            if result.status != "SUCCEEDED":
                raise _NodeFailure(str(result.status))
            return result.output, True
        except asyncio.CancelledError:
            token.cancel()
            await self._skill_invoker.cancel(binding, invocation_id)
            raise
        finally:
            state.active_invocations.pop(node_id, None)

    async def _run_condition(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        selected_then = evaluate_expression(str(node["expression"]), state.context())
        selected_key, skipped_key = ("then", "else") if selected_then else ("else", "then")
        selected = cast(tuple[str, ...], node[selected_key])
        skipped = cast(tuple[str, ...], node[skipped_key])
        for target in skipped:
            if target not in state.executed:
                await self._skip_node(state, target, f"condition {node_id} did not select branch")
        return {"matched": selected_then, "selected": list(selected)}, True

    async def _run_parallel(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        branches = cast(tuple[tuple[str, ...], ...], node["branches"])
        policy = str(node["failure_policy"])
        async def run_branch(index: int, branch: tuple[str, ...]) -> dict[str, object]:
            statuses: list[str] = []
            branch_ok = True
            for target in branch:
                status = await self._execute_node(state, target, active_controller=node_id)
                statuses.append(status.value)
                if status in {NodeRunStatus.FAILED, NodeRunStatus.TIMED_OUT, NodeRunStatus.REQUIRES_REVIEW}:
                    branch_ok = False
                    break
            return {
                "index": index,
                "status": "SUCCEEDED" if branch_ok else "FAILED",
                "nodes": statuses,
            }

        tasks = [asyncio.create_task(run_branch(index, branch)) for index, branch in enumerate(branches)]
        if policy == "fail_fast":
            pending = set(tasks)
            branch_results: list[dict[str, object]] = []
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                results = [task.result() for task in done]
                branch_results.extend(results)
                if any(result["status"] == "FAILED" for result in results):
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    break
        else:
            branch_results = list(await asyncio.gather(*tasks))
        branch_results.sort(key=lambda item: cast(int, item["index"]))
        for index, branch in enumerate(branches):
            result = next((item for item in branch_results if item["index"] == index), None)
            if result is None:
                for target in branch:
                    if state.statuses[target] in {NodeRunStatus.PENDING, NodeRunStatus.READY}:
                        await self._skip_node(state, target, "parallel fail_fast cancelled branch")
                branch_results.append(
                    {"index": index, "status": "CANCELLED", "nodes": [state.statuses[x].value for x in branch]}
                )
        branch_results.sort(key=lambda item: cast(int, item["index"]))
        success_count = sum(item["status"] == "SUCCEEDED" for item in branch_results)
        failed_nodes = {
            target
            for branch in branches
            for target in branch
            if state.statuses[target] in {NodeRunStatus.FAILED, NodeRunStatus.TIMED_OUT}
        }
        if policy == "min_success":
            succeeded = success_count >= cast(int, node["min_success"])
            if succeeded:
                state.tolerated_failures.update(failed_nodes)
        else:
            succeeded = success_count == len(branches)
        return {"branches": branch_results, "success_count": success_count}, succeeded

    async def _run_delay(self, node: Mapping[str, object]) -> tuple[object, bool]:
        if "duration_seconds" in node:
            seconds = float(cast(int | float, node["duration_seconds"]))
        else:
            until = datetime.fromisoformat(str(node["until"]))
            if until.tzinfo is None or until.utcoffset() is None:
                raise WorkflowExecutionError("delay until must be timezone-aware")
            seconds = max(0.0, (until.astimezone(UTC) - self._clock.now()).total_seconds())
        await self._clock.sleep(seconds)
        return {"delayed_seconds": seconds}, True

    async def _run_sub_workflow(
        self, state: _ExecutionState, node_id: str, node: Mapping[str, object]
    ) -> tuple[object, bool]:
        workflow_id, version = str(node["workflow_id"]), str(node["workflow_version"])
        child = self._registry.get(workflow_id, version)
        parameters = _resolve_value(node["input"], state.context())
        if not isinstance(parameters, Mapping):
            raise WorkflowExecutionError("sub-workflow input must resolve to an object")
        child_run_id = self._new_id()
        request = state.request
        async with self._database.transaction() as transaction:
            await WorkflowRunRepository(transaction).create_workflow(
                run_id=child_run_id,
                task_id=request.task_id,
                workflow_id=workflow_id,
                workflow_version=version,
                workflow_digest=child.digest,
                input_digest=_json_digest(parameters),
                deadline=request.deadline,
                created_at=self._clock.now(),
                correlation_id=request.correlation_id,
                transition_id=self._new_id(),
                event_id=self._new_id(),
                parent_run_id=request.run_id,
            )
        result = await self.execute(
            WorkflowExecutionRequest(
                child_run_id,
                request.task_id,
                child,
                parameters,
                request.bindings,
                request.deadline,
                request.correlation_id,
                request.allowed_permissions,
                request.depth + 1,
                request.cancellation,
            )
        )
        succeeded = result.status is WorkflowRunStatus.SUCCEEDED
        if not succeeded and node["failure_policy"] == "continue":
            succeeded = True
        if not succeeded and node["failure_policy"] == "compensate":
            compensation = str(node["compensation_node"])
            compensation_status = await self._execute_node(
                state, compensation, active_controller=node_id
            )
            succeeded = False
            if compensation_status is not NodeRunStatus.SUCCEEDED:
                raise _NodeFailure("COMPENSATION_FAILED")
        return {"run_id": child_run_id, "status": result.status.value, "output": dict(result.output)}, succeeded

    async def _complete_node(self, state: _ExecutionState, node_id: str, output: object) -> None:
        encoded = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
        async with state.lock, self._database.transaction() as transaction:
            repository = WorkflowRunRepository(transaction)
            attempt = state.attempts[node_id]
            node = await repository.get_node(state.request.run_id, node_id, attempt)
            payload = await self._artifacts.store_if_large(
                transaction,
                encoded,
                artifact_id=self._new_id(),
                media_type="application/json",
                created_at=self._clock.now(),
                correlation_id=state.request.correlation_id,
            )
            await repository.transition_node(
                state.request.run_id,
                node_id,
                attempt,
                NodeRunStatus.SUCCEEDED,
                expected_version=node.version,
                reason="node completed",
                occurred_at=self._clock.now(),
                transition_id=self._new_id(),
                event_id=self._new_id(),
                output_artifact_id=None if payload.artifact is None else payload.artifact.artifact_id,
                inline_output=output if payload.inline is not None else None,
            )
        state.statuses[node_id] = NodeRunStatus.SUCCEEDED
        state.executed.add(node_id)

    async def _fail_node(self, state: _ExecutionState, node_id: str, reason: str) -> None:
        await self._transition_node(
            state, node_id, NodeRunStatus.FAILED, reason, error_id=self._new_id()
        )

    async def _timeout_node(self, state: _ExecutionState, node_id: str, reason: str) -> None:
        await self._transition_node(
            state, node_id, NodeRunStatus.TIMED_OUT, reason, error_id=self._new_id()
        )

    async def _cancel_remaining(self, state: _ExecutionState, reason: str) -> None:
        for node_id in state.nodes:
            if node_id in state.executed:
                continue
            status = state.statuses[node_id]
            if status in {NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING}:
                await self._transition_node(state, node_id, NodeRunStatus.CANCELLED, reason)

    def _remaining(self, state: _ExecutionState) -> float:
        return (state.request.deadline.astimezone(UTC) - self._clock.now()).total_seconds()

    def _guard(self, state: _ExecutionState) -> None:
        if state.request.cancellation is not None and state.request.cancellation.cancelled:
            raise _WorkflowCancelled
        if self._remaining(state) <= 0:
            raise _WorkflowTimedOut

    async def _skip_node(self, state: _ExecutionState, node_id: str, reason: str) -> None:
        await self._transition_node(state, node_id, NodeRunStatus.SKIPPED, reason)

    async def _transition_node(
        self,
        state: _ExecutionState,
        node_id: str,
        status: NodeRunStatus,
        reason: str,
        *,
        error_id: str | None = None,
    ) -> NodeRun:
        async with state.lock, self._database.transaction() as transaction:
            repository = WorkflowRunRepository(transaction)
            attempt = state.attempts.get(node_id, 1)
            current = await repository.get_node(state.request.run_id, node_id, attempt)
            updated = await repository.transition_node(
                state.request.run_id,
                node_id,
                attempt,
                status,
                expected_version=current.version,
                reason=reason,
                occurred_at=self._clock.now(),
                transition_id=self._new_id(),
                event_id=self._new_id(),
                error_id=error_id,
            )
        state.statuses[node_id] = status
        if status in NODE_TERMINAL:
            state.executed.add(node_id)
        return updated

    async def _finish_workflow(
        self,
        run_id: str,
        status: WorkflowRunStatus,
        reason: str,
        error_id: str | None,
    ) -> None:
        async with self._database.transaction() as transaction:
            repository = WorkflowRunRepository(transaction)
            current = await repository.get_workflow(run_id)
            await repository.transition_workflow(
                run_id,
                status,
                expected_version=current.version,
                reason=reason,
                occurred_at=self._clock.now(),
                transition_id=self._new_id(),
                event_id=self._new_id(),
                error_id=error_id,
            )

    def _new_id(self) -> str:
        return str(self._identifiers.new())


def _resolve_value(value: object, context: Mapping[str, object]) -> object:
    if isinstance(value, str) and value.startswith("$."):
        found, resolved = resolve_json_path(context, value)
        if not found:
            raise WorkflowExecutionError(f"workflow reference not found: {value}")
        return resolved
    if isinstance(value, Mapping):
        return {str(key): _resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_resolve_value(item, context) for item in value]
    return value


def _json_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
