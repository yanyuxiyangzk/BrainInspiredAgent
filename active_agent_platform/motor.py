"""F06 Grant-only MotorExec and persistent logical Task projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from active_agent_platform.planning import GrantIssuer, GrantStatus, PlanningError
from active_agent_platform.skill_recovery import RecoveryAction, SkillRecoveryResult
from active_agent_platform.skills import SkillBinding
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from active_agent_platform.workflow import WorkflowDefinition
from active_agent_platform.workflow_runs import WorkflowRunRepository, WorkflowRunStatus
from active_agent_platform.workflow_runtime import (
    WorkflowExecutionRequest,
    WorkflowExecutionResult,
    WorkflowRuntime,
)
from brain_kernel.ports import Clock, UuidGenerator


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    grant_id: str
    status: TaskStatus
    version: int
    attempt: int
    deadline: datetime
    correlation_id: str


@dataclass(frozen=True, slots=True)
class MotorExecutionRequest:
    grant_id: str
    task_id: str
    workflow: WorkflowDefinition
    parameters: Mapping[str, object]
    bindings: Mapping[tuple[str, str, str], SkillBinding]
    deadline: datetime
    allowed_permissions: frozenset[str]
    attempt: int = 1
    priority: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class MotorRecoveryResult:
    task: TaskRecord
    execution: WorkflowExecutionResult | None = None


class MotorExec:
    def __init__(
        self,
        database: SQLiteDatabase,
        runtime: WorkflowRuntime,
        *,
        clock: Clock,
        identifiers: UuidGenerator,
    ) -> None:
        self._database = database
        self._runtime = runtime
        self._clock = clock
        self._identifiers = identifiers

    async def execute(self, request: MotorExecutionRequest) -> WorkflowExecutionResult:
        now = self._clock.now()
        run_id = self._new_id()
        async with self._database.transaction() as transaction:
            issuer = GrantIssuer(transaction)
            grant = await issuer.get(request.grant_id)
            self._validate_grant(grant.document, grant.status, request, now)
            await issuer.authorize_attempt(
                request.grant_id, request.task_id, request.attempt, authorized_at=now
            )
            task = await self._prepare_task(transaction, request, grant.correlation_id, now)
            task = await self._transition(transaction, task, TaskStatus.READY, "task admitted", now)
            task = await self._transition(
                transaction, task, TaskStatus.DISPATCHED, "workflow dispatched", now
            )
            await self._transition(transaction, task, TaskStatus.RUNNING, "workflow started", now)
            await WorkflowRunRepository(transaction).create_workflow(
                run_id=run_id,
                task_id=request.task_id,
                workflow_id=request.workflow.workflow_id,
                workflow_version=request.workflow.version,
                workflow_digest=request.workflow.digest,
                input_digest=_digest(request.parameters),
                deadline=request.deadline,
                created_at=now,
                correlation_id=grant.correlation_id,
                transition_id=self._new_id(),
                event_id=self._new_id(),
            )
        result = await self._runtime.execute(
            WorkflowExecutionRequest(
                run_id,
                request.task_id,
                request.workflow,
                request.parameters,
                request.bindings,
                request.deadline,
                grant.correlation_id,
                request.allowed_permissions,
            )
        )
        target = {
            WorkflowRunStatus.SUCCEEDED: TaskStatus.SUCCEEDED,
            WorkflowRunStatus.FAILED: TaskStatus.FAILED,
            WorkflowRunStatus.TIMED_OUT: TaskStatus.TIMED_OUT,
            WorkflowRunStatus.CANCELLED: TaskStatus.CANCELLED,
        }[result.status]
        async with self._database.transaction() as transaction:
            task = await self._get_task(transaction, request.task_id)
            await self._transition(transaction, task, target, "workflow reached terminal state", self._clock.now())
        return result

    async def execute_batch(
        self, requests: tuple[MotorExecutionRequest, ...]
    ) -> tuple[WorkflowExecutionResult, ...]:
        """Dispatch higher priority first, using task_id as the deterministic tie-breaker."""
        ordered = sorted(requests, key=lambda item: (-item.priority, item.task_id))
        return tuple([await self.execute(request) for request in ordered])

    async def recover(
        self, request: MotorExecutionRequest, recovery: SkillRecoveryResult
    ) -> MotorRecoveryResult:
        async with self._database.transaction() as transaction:
            task = await self._get_task(transaction, request.task_id)
            if task.status is not TaskStatus.RUNNING:
                raise PlanningError("TASK_STATE_TRANSITION_INVALID", "only an interrupted RUNNING task can recover")
            if recovery.action is RecoveryAction.REPLAY:
                task = await self._transition(
                    transaction, task, TaskStatus.FAILED, "interrupted attempt will replay", self._clock.now()
                )
            else:
                target = {
                    RecoveryAction.COMPLETE: TaskStatus.SUCCEEDED,
                    RecoveryAction.FAIL: TaskStatus.FAILED,
                    RecoveryAction.REQUIRE_REVIEW: TaskStatus.REQUIRES_REVIEW,
                    RecoveryAction.TIME_OUT: TaskStatus.TIMED_OUT,
                }.get(recovery.action)
                if target is None:
                    raise PlanningError("TASK_RECOVERY_INVALID", "unsupported recovery action")
                task = await self._transition(
                    transaction, task, target, recovery.reason, self._clock.now()
                )
                return MotorRecoveryResult(task)
        next_attempt = recovery.next_attempt
        if next_attempt is None:
            raise PlanningError("TASK_RECOVERY_INVALID", "replay requires next_attempt")
        execution = await self.execute(replace(request, attempt=next_attempt))
        async with self._database.transaction() as transaction:
            task = await self._get_task(transaction, request.task_id)
        return MotorRecoveryResult(task, execution)

    async def cancel(self, task_id: str) -> bool:
        row = await self._database.fetch_one(
            "SELECT run_id FROM workflow_run WHERE task_id = ? AND status = 'RUNNING' ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        return False if row is None else await self._runtime.cancel(str(row["run_id"]))

    async def _prepare_task(
        self,
        transaction: SQLiteTransaction,
        request: MotorExecutionRequest,
        correlation_id: str,
        now: datetime,
    ) -> TaskRecord:
        row = await transaction.fetch_one("SELECT * FROM task WHERE task_id = ?", (request.task_id,))
        if row is None:
            await transaction.execute(
                """
                INSERT INTO task(task_id, grant_id, status, version, attempt, created_at, deadline, correlation_id)
                VALUES (?, ?, 'PENDING', 0, ?, ?, ?, ?)
                """,
                (
                    request.task_id,
                    request.grant_id,
                    request.attempt,
                    _time(now),
                    _time(request.deadline),
                    correlation_id,
                ),
            )
            task = await self._get_task(transaction, request.task_id)
            await self._record_transition(transaction, task, None, TaskStatus.PENDING, "task created", now)
            return task
        task = _task(row)
        if task.grant_id != request.grant_id or request.attempt != task.attempt + 1:
            raise PlanningError("TASK_ATTEMPT_INVALID", "logical task attempt is invalid")
        if task.status not in {TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.REQUIRES_REVIEW}:
            raise PlanningError("TASK_STATE_TRANSITION_INVALID", "task is not recoverable")
        await transaction.execute(
            "UPDATE task SET status = 'PENDING', attempt = ?, version = version + 1, finished_at = NULL, error_id = NULL WHERE task_id = ?",
            (request.attempt, request.task_id),
        )
        return await self._get_task(transaction, request.task_id)

    def _validate_grant(
        self,
        document: Mapping[str, object],
        status: GrantStatus,
        request: MotorExecutionRequest,
        now: datetime,
    ) -> None:
        if status is not GrantStatus.ACTIVE:
            raise PlanningError("GRANT_NOT_ACTIVE", "MotorExec requires an active grant")
        if request.task_id != document["task_id"]:
            raise PlanningError("GRANT_TASK_MISMATCH", "task does not match grant")
        workflow = document["workflow"]
        if not isinstance(workflow, Mapping) or (
            workflow.get("workflow_id"), workflow.get("version"), workflow.get("digest")
        ) != (request.workflow.workflow_id, request.workflow.version, request.workflow.digest):
            raise PlanningError("GRANT_WORKFLOW_MISMATCH", "workflow does not match grant")
        expires = datetime.fromisoformat(str(document["expires_at"]))
        if now >= expires or request.deadline > expires:
            raise PlanningError("GRANT_EXPIRED", "grant cannot cover task deadline")
        permissions = document["allowed_permissions"]
        if not isinstance(permissions, tuple | list):
            raise PlanningError("GRANT_INVALID", "grant permissions are invalid")
        if not request.allowed_permissions <= frozenset(str(item) for item in permissions):
            raise PlanningError("GRANT_PERMISSION_DENIED", "execution expands grant permissions")
        raw_bindings = document["bindings"]
        if not isinstance(raw_bindings, tuple | list) or not all(
            isinstance(item, Mapping) for item in raw_bindings
        ):
            raise PlanningError("GRANT_INVALID", "grant bindings are invalid")
        expected = {
            (str(item["node_id"]), str(item["skill_id"]), str(item["skill_version"]), str(item["skill_digest"]))
            for item in raw_bindings
            if isinstance(item, Mapping)
        }
        actual = {
            (binding.node_id, binding.skill_id, binding.skill_version, binding.skill_digest)
            for binding in request.bindings.values()
        }
        if actual != expected:
            raise PlanningError("GRANT_BINDING_MISMATCH", "Skill bindings do not match grant")

    async def _transition(
        self,
        transaction: SQLiteTransaction,
        task: TaskRecord,
        target: TaskStatus,
        reason: str,
        now: datetime,
    ) -> TaskRecord:
        allowed = {
            TaskStatus.PENDING: {TaskStatus.READY},
            TaskStatus.READY: {TaskStatus.DISPATCHED},
            TaskStatus.DISPATCHED: {TaskStatus.RUNNING},
            TaskStatus.RUNNING: {
                TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.TIMED_OUT,
                TaskStatus.CANCELLED, TaskStatus.REQUIRES_REVIEW,
            },
        }
        if target not in allowed.get(task.status, set()):
            raise PlanningError("TASK_STATE_TRANSITION_INVALID", "invalid task transition")
        version = task.version + 1
        finished = _time(now) if target in {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED, TaskStatus.REQUIRES_REVIEW,
        } else None
        started = _time(now) if target is TaskStatus.RUNNING else None
        cursor = await transaction.execute(
            "UPDATE task SET status = ?, version = ?, started_at = COALESCE(?, started_at), finished_at = ? WHERE task_id = ? AND version = ?",
            (target, version, started, finished, task.task_id, task.version),
        )
        if cursor.rowcount != 1:
            raise PlanningError("TASK_VERSION_CONFLICT", "task projection changed concurrently")
        await self._record_transition(transaction, task, task.status, target, reason, now)
        return await self._get_task(transaction, task.task_id)

    async def _record_transition(
        self, transaction: SQLiteTransaction, task: TaskRecord,
        previous: TaskStatus | None, target: TaskStatus, reason: str, now: datetime,
    ) -> None:
        await transaction.execute(
            "INSERT INTO task_transition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._new_id(), task.task_id, previous, target, reason, task.attempt, self._new_id(), _time(now), task.correlation_id),
        )

    async def _get_task(self, transaction: SQLiteTransaction, task_id: str) -> TaskRecord:
        row = await transaction.fetch_one("SELECT * FROM task WHERE task_id = ?", (task_id,))
        if row is None:
            raise PlanningError("TASK_NOT_FOUND", "task not found")
        return _task(row)

    def _new_id(self) -> str:
        return str(self._identifiers.new())


def _task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(str(row["task_id"]), str(row["grant_id"]), TaskStatus(str(row["status"])), int(row["version"]), int(row["attempt"]), datetime.fromisoformat(str(row["deadline"])).astimezone(UTC), str(row["correlation_id"]))


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
