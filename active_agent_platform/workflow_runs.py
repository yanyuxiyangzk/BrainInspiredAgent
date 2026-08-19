"""Persistent WorkflowRun and NodeRun projections with append-only transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from active_agent_platform.storage import SQLiteTransaction


class WorkflowRunStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class NodeRunStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


WORKFLOW_TERMINAL = frozenset(
    {
        WorkflowRunStatus.SUCCEEDED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.TIMED_OUT,
        WorkflowRunStatus.CANCELLED,
    }
)
NODE_TERMINAL = frozenset(
    {
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.FAILED,
        NodeRunStatus.TIMED_OUT,
        NodeRunStatus.CANCELLED,
        NodeRunStatus.SKIPPED,
        NodeRunStatus.REQUIRES_REVIEW,
    }
)

_WORKFLOW_TRANSITIONS: Mapping[WorkflowRunStatus, frozenset[WorkflowRunStatus]] = {
    WorkflowRunStatus.PENDING: frozenset(
        {WorkflowRunStatus.READY, WorkflowRunStatus.CANCELLED}
    ),
    WorkflowRunStatus.READY: frozenset(
        {
            WorkflowRunStatus.RUNNING,
            WorkflowRunStatus.CANCELLED,
            WorkflowRunStatus.TIMED_OUT,
        }
    ),
    WorkflowRunStatus.RUNNING: WORKFLOW_TERMINAL,
}
_NODE_TRANSITIONS: Mapping[NodeRunStatus, frozenset[NodeRunStatus]] = {
    NodeRunStatus.PENDING: frozenset(
        {NodeRunStatus.READY, NodeRunStatus.SKIPPED, NodeRunStatus.CANCELLED}
    ),
    NodeRunStatus.READY: frozenset(
        {
            NodeRunStatus.RUNNING,
            NodeRunStatus.SKIPPED,
            NodeRunStatus.CANCELLED,
            NodeRunStatus.TIMED_OUT,
        }
    ),
    NodeRunStatus.RUNNING: NODE_TERMINAL,
}


class RunStateError(ValueError):
    def __init__(self, message: str, *, code: str = "TASK_STATE_TRANSITION_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: str
    task_id: str
    workflow_id: str
    workflow_version: str
    workflow_digest: str
    input_digest: str
    status: WorkflowRunStatus
    version: int
    parent_run_id: str | None
    deadline: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_id: str | None
    correlation_id: str


@dataclass(frozen=True, slots=True)
class NodeRun:
    run_id: str
    node_id: str
    attempt: int
    status: NodeRunStatus
    version: int
    skill_binding_id: str | None
    input_artifact_id: str | None
    output_artifact_id: str | None
    output: object | None
    error_id: str | None
    usage: Mapping[str, object] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RunTransition:
    transition_id: str
    from_status: str | None
    to_status: str
    reason: str
    version: int
    event_id: str
    occurred_at: datetime
    error_id: str | None
    correlation_id: str


class WorkflowRunRepository:
    """Uses the caller transaction so projection and later Outbox writes remain atomic."""

    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def create_workflow(
        self,
        *,
        run_id: str,
        task_id: str,
        workflow_id: str,
        workflow_version: str,
        workflow_digest: str,
        input_digest: str,
        deadline: datetime,
        created_at: datetime,
        correlation_id: str,
        transition_id: str,
        event_id: str,
        parent_run_id: str | None = None,
    ) -> WorkflowRun:
        _identity(run_id, task_id, workflow_id, workflow_version, correlation_id)
        deadline_value, created_value = _timestamp(deadline), _timestamp(created_at)
        if deadline <= created_at:
            raise RunStateError("workflow deadline must be after creation")
        try:
            await self._transaction.execute(
                """
                INSERT INTO workflow_run(
                    run_id, task_id, workflow_id, workflow_version, workflow_digest,
                    input_digest, status, parent_run_id, deadline, created_at,
                    correlation_id, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    run_id,
                    task_id,
                    workflow_id,
                    workflow_version,
                    workflow_digest,
                    input_digest,
                    WorkflowRunStatus.PENDING,
                    parent_run_id,
                    deadline_value,
                    created_value,
                    correlation_id,
                ),
            )
            await self._transaction.execute(
                """
                INSERT INTO workflow_run_transition(
                    transition_id, run_id, from_status, to_status, reason, version,
                    event_id, occurred_at, error_id, correlation_id
                ) VALUES (?, ?, NULL, ?, 'created', 0, ?, ?, NULL, ?)
                """,
                (
                    transition_id,
                    run_id,
                    WorkflowRunStatus.PENDING,
                    event_id,
                    created_value,
                    correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RunStateError("workflow run or creation event already exists") from error
        return await self.get_workflow(run_id)

    async def create_node(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        created_at: datetime,
        correlation_id: str,
        transition_id: str,
        event_id: str,
        skill_binding_id: str | None = None,
        input_artifact_id: str | None = None,
    ) -> NodeRun:
        _identity(run_id, node_id, correlation_id)
        if attempt < 1:
            raise RunStateError("node attempt must be positive")
        previous = await self._transaction.fetch_one(
            """
            SELECT attempt, status FROM node_run
            WHERE run_id = ? AND node_id = ? ORDER BY attempt DESC LIMIT 1
            """,
            (run_id, node_id),
        )
        if previous is None and attempt != 1 or previous is not None and (
            attempt != int(previous["attempt"]) + 1
            or NodeRunStatus(str(previous["status"])) not in NODE_TERMINAL
        ):
            raise RunStateError("node attempts must be sequential and follow a terminal attempt")
        occurred_at = _timestamp(created_at)
        try:
            await self._transaction.execute(
                """
                INSERT INTO node_run(
                    run_id, node_id, attempt, skill_binding_id, status,
                    input_artifact_id, correlation_id, version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    run_id,
                    node_id,
                    attempt,
                    skill_binding_id,
                    NodeRunStatus.PENDING,
                    input_artifact_id,
                    correlation_id,
                    occurred_at,
                ),
            )
            await self._transaction.execute(
                """
                INSERT INTO node_run_transition(
                    transition_id, run_id, node_id, attempt, from_status, to_status,
                    reason, version, event_id, occurred_at, error_id, correlation_id
                ) VALUES (?, ?, ?, ?, NULL, ?, 'created', 0, ?, ?, NULL, ?)
                """,
                (
                    transition_id,
                    run_id,
                    node_id,
                    attempt,
                    NodeRunStatus.PENDING,
                    event_id,
                    occurred_at,
                    correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RunStateError("node run or creation event already exists") from error
        return await self.get_node(run_id, node_id, attempt)

    async def transition_workflow(
        self,
        run_id: str,
        to_status: WorkflowRunStatus,
        *,
        expected_version: int,
        reason: str,
        occurred_at: datetime,
        transition_id: str,
        event_id: str,
        error_id: str | None = None,
    ) -> WorkflowRun:
        current = await self.get_workflow(run_id)
        duplicate = await self._workflow_event(event_id)
        if duplicate is not None:
            if duplicate["run_id"] == run_id and duplicate["to_status"] == to_status:
                return current
            raise RunStateError("event ID already belongs to another transition")
        _transition_allowed(current.status, to_status, _WORKFLOW_TRANSITIONS)
        _transition_fields(to_status, reason, error_id)
        if current.version != expected_version:
            raise RunStateError("workflow projection version conflict")
        next_version = expected_version + 1
        timestamp = _timestamp(occurred_at)
        started_at = occurred_at.astimezone(UTC) if to_status is WorkflowRunStatus.RUNNING else current.started_at
        finished_at = timestamp if to_status in WORKFLOW_TERMINAL else None
        cursor = await self._transaction.execute(
            """
            UPDATE workflow_run
            SET status = ?, version = ?, started_at = ?, finished_at = ?, error_id = ?
            WHERE run_id = ? AND version = ? AND status = ?
            """,
            (
                to_status,
                next_version,
                _optional_timestamp(started_at),
                finished_at,
                error_id,
                run_id,
                expected_version,
                current.status,
            ),
        )
        if cursor.rowcount != 1:
            raise RunStateError("workflow compare-and-swap failed")
        await self._transaction.execute(
            """
            INSERT INTO workflow_run_transition(
                transition_id, run_id, from_status, to_status, reason, version,
                event_id, occurred_at, error_id, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                run_id,
                current.status,
                to_status,
                reason,
                next_version,
                event_id,
                timestamp,
                error_id,
                current.correlation_id,
            ),
        )
        return await self.get_workflow(run_id)

    async def transition_node(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        to_status: NodeRunStatus,
        *,
        expected_version: int,
        reason: str,
        occurred_at: datetime,
        transition_id: str,
        event_id: str,
        error_id: str | None = None,
        output_artifact_id: str | None = None,
        inline_output: object | None = None,
        usage: Mapping[str, object] | None = None,
    ) -> NodeRun:
        current = await self.get_node(run_id, node_id, attempt)
        duplicate = await self._node_event(event_id)
        if duplicate is not None:
            if (
                duplicate["run_id"] == run_id
                and duplicate["node_id"] == node_id
                and duplicate["attempt"] == attempt
                and duplicate["to_status"] == to_status
            ):
                return current
            raise RunStateError("event ID already belongs to another transition")
        _transition_allowed(current.status, to_status, _NODE_TRANSITIONS)
        _transition_fields(to_status, reason, error_id)
        if current.version != expected_version:
            raise RunStateError("node projection version conflict")
        if output_artifact_id is not None and to_status is not NodeRunStatus.SUCCEEDED:
            raise RunStateError("only a successful node can attach output")
        if inline_output is not None and to_status is not NodeRunStatus.SUCCEEDED:
            raise RunStateError("only a successful node can attach inline output")
        if inline_output is not None and output_artifact_id is not None:
            raise RunStateError("node output must be inline or artifact-backed, not both")
        next_version = expected_version + 1
        timestamp = _timestamp(occurred_at)
        started_at = occurred_at.astimezone(UTC) if to_status is NodeRunStatus.RUNNING else current.started_at
        finished_at = timestamp if to_status in NODE_TERMINAL else None
        usage_json = None if usage is None else json.dumps(usage, sort_keys=True, separators=(",", ":"))
        output_json = None if inline_output is None else json.dumps(
            inline_output, sort_keys=True, separators=(",", ":")
        )
        if output_json is not None and len(output_json.encode("utf-8")) > 1_048_576:
            raise RunStateError("inline node output exceeds 1 MiB")
        cursor = await self._transaction.execute(
            """
            UPDATE node_run
            SET status = ?, version = ?, started_at = ?, finished_at = ?, error_id = ?,
                output_artifact_id = COALESCE(?, output_artifact_id),
                output_json = COALESCE(?, output_json), usage_json = COALESCE(?, usage_json)
            WHERE run_id = ? AND node_id = ? AND attempt = ? AND version = ? AND status = ?
            """,
            (
                to_status,
                next_version,
                _optional_timestamp(started_at),
                finished_at,
                error_id,
                output_artifact_id,
                output_json,
                usage_json,
                run_id,
                node_id,
                attempt,
                expected_version,
                current.status,
            ),
        )
        if cursor.rowcount != 1:
            raise RunStateError("node compare-and-swap failed")
        await self._transaction.execute(
            """
            INSERT INTO node_run_transition(
                transition_id, run_id, node_id, attempt, from_status, to_status,
                reason, version, event_id, occurred_at, error_id, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                run_id,
                node_id,
                attempt,
                current.status,
                to_status,
                reason,
                next_version,
                event_id,
                timestamp,
                error_id,
                current.correlation_id,
            ),
        )
        return await self.get_node(run_id, node_id, attempt)

    async def get_workflow(self, run_id: str) -> WorkflowRun:
        row = await self._transaction.fetch_one(
            "SELECT * FROM workflow_run WHERE run_id = ?", (run_id,)
        )
        if row is None:
            raise RunStateError("workflow run not found", code="WORKFLOW_RUN_NOT_FOUND")
        return _workflow(row)

    async def get_node(self, run_id: str, node_id: str, attempt: int) -> NodeRun:
        row = await self._transaction.fetch_one(
            "SELECT * FROM node_run WHERE run_id = ? AND node_id = ? AND attempt = ?",
            (run_id, node_id, attempt),
        )
        if row is None:
            raise RunStateError("node run not found", code="NODE_RUN_NOT_FOUND")
        return _node(row)

    async def workflow_history(self, run_id: str) -> tuple[RunTransition, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT * FROM workflow_run_transition WHERE run_id = ? ORDER BY version",
            (run_id,),
        )
        return tuple(_transition(row) for row in rows)

    async def node_history(
        self, run_id: str, node_id: str, attempt: int
    ) -> tuple[RunTransition, ...]:
        rows = await self._transaction.fetch_all(
            """
            SELECT * FROM node_run_transition
            WHERE run_id = ? AND node_id = ? AND attempt = ? ORDER BY version
            """,
            (run_id, node_id, attempt),
        )
        return tuple(_transition(row) for row in rows)

    async def _workflow_event(self, event_id: str) -> sqlite3.Row | None:
        return await self._transaction.fetch_one(
            "SELECT run_id, to_status FROM workflow_run_transition WHERE event_id = ?",
            (event_id,),
        )

    async def _node_event(self, event_id: str) -> sqlite3.Row | None:
        return await self._transaction.fetch_one(
            """
            SELECT run_id, node_id, attempt, to_status
            FROM node_run_transition WHERE event_id = ?
            """,
            (event_id,),
        )


_Status = TypeVar("_Status", WorkflowRunStatus, NodeRunStatus)


def _transition_allowed(
    current: _Status,
    target: _Status,
    transitions: Mapping[_Status, frozenset[_Status]],
) -> None:
    if target not in transitions.get(current, frozenset()):
        raise RunStateError(f"invalid transition: {current} -> {target}")


def _transition_fields(
    status: WorkflowRunStatus | NodeRunStatus, reason: str, error_id: str | None
) -> None:
    if not reason or len(reason) > 500:
        raise RunStateError("transition reason must contain 1 to 500 characters")
    error_states = {
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.TIMED_OUT,
        NodeRunStatus.FAILED,
        NodeRunStatus.TIMED_OUT,
        NodeRunStatus.REQUIRES_REVIEW,
    }
    if status in error_states and not error_id:
        raise RunStateError("failure transition requires error_id")
    if status not in error_states and error_id is not None:
        raise RunStateError("non-failure transition cannot attach error_id")


def _identity(*values: str) -> None:
    if any(not value or len(value) > 255 for value in values):
        raise RunStateError("identifiers must contain 1 to 255 characters")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunStateError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _workflow(row: sqlite3.Row) -> WorkflowRun:
    return WorkflowRun(
        str(row["run_id"]),
        str(row["task_id"]),
        str(row["workflow_id"]),
        str(row["workflow_version"]),
        str(row["workflow_digest"]),
        str(row["input_digest"]),
        WorkflowRunStatus(str(row["status"])),
        int(row["version"]),
        None if row["parent_run_id"] is None else str(row["parent_run_id"]),
        _datetime(row["deadline"]),
        _datetime(row["created_at"]),
        _optional_datetime(row["started_at"]),
        _optional_datetime(row["finished_at"]),
        None if row["error_id"] is None else str(row["error_id"]),
        str(row["correlation_id"]),
    )


def _node(row: sqlite3.Row) -> NodeRun:
    usage = None
    if row["usage_json"] is not None:
        decoded = json.loads(str(row["usage_json"]))
        if not isinstance(decoded, dict):
            raise RunStateError("stored node usage must be an object")
        usage = decoded
    output = None if row["output_json"] is None else json.loads(str(row["output_json"]))
    return NodeRun(
        str(row["run_id"]),
        str(row["node_id"]),
        int(row["attempt"]),
        NodeRunStatus(str(row["status"])),
        int(row["version"]),
        None if row["skill_binding_id"] is None else str(row["skill_binding_id"]),
        None if row["input_artifact_id"] is None else str(row["input_artifact_id"]),
        None if row["output_artifact_id"] is None else str(row["output_artifact_id"]),
        output,
        None if row["error_id"] is None else str(row["error_id"]),
        usage,
        _datetime(row["created_at"]),
        _optional_datetime(row["started_at"]),
        _optional_datetime(row["finished_at"]),
        str(row["correlation_id"]),
    )


def _transition(row: sqlite3.Row) -> RunTransition:
    return RunTransition(
        str(row["transition_id"]),
        None if row["from_status"] is None else str(row["from_status"]),
        str(row["to_status"]),
        str(row["reason"]),
        int(row["version"]),
        str(row["event_id"]),
        _datetime(row["occurred_at"]),
        None if row["error_id"] is None else str(row["error_id"]),
        str(row["correlation_id"]),
    )
