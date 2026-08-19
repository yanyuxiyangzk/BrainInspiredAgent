"""G01 persistent Episode/audit facts and correlation-oriented Trace queries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction


@dataclass(frozen=True, slots=True)
class TraceBundle:
    correlation_id: str
    plans: tuple[Mapping[str, object], ...]
    decisions: tuple[Mapping[str, object], ...]
    grants: tuple[Mapping[str, object], ...]
    tasks: tuple[Mapping[str, object], ...]
    workflow_runs: tuple[Mapping[str, object], ...]
    node_runs: tuple[Mapping[str, object], ...]
    episodes: tuple[Mapping[str, object], ...]
    dna_contexts: tuple[Mapping[str, object], ...]
    audits: tuple[Mapping[str, object], ...]


class TraceRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def add_episode(
        self, episode_id: str, task_id: str, document: Mapping[str, object], *,
        created_at: datetime, correlation_id: str,
    ) -> None:
        await self._transaction.execute(
            "INSERT INTO episode VALUES (?, ?, ?, ?, ?)",
            (episode_id, task_id, _json(document), _time(created_at), correlation_id),
        )

    async def audit(
        self, action: str, subject_type: str, subject_id: str, document: Mapping[str, object], *,
        occurred_at: datetime, correlation_id: str,
    ) -> str:
        previous = await self._transaction.fetch_one(
            "SELECT audit_id FROM audit_record WHERE subject_type = ? AND subject_id = ? ORDER BY occurred_at DESC LIMIT 1",
            (subject_type, subject_id),
        )
        previous_id = None if previous is None else str(previous["audit_id"])
        audit_id = hashlib.sha256(
            f"{action}:{subject_type}:{subject_id}:{_time(occurred_at)}:{previous_id}".encode()
        ).hexdigest()
        await self._transaction.execute(
            "INSERT INTO audit_record VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (audit_id, action, subject_type, subject_id, previous_id, _json(document), _time(occurred_at), correlation_id),
        )
        return audit_id


class TraceQuery:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def by_correlation(self, correlation_id: str) -> TraceBundle:
        if not correlation_id:
            raise ValueError("correlation_id must not be empty")
        return TraceBundle(
            correlation_id,
            await self._documents("plan", "plan_json", correlation_id),
            await self._documents("plan_decision", "decision_json", correlation_id),
            await self._documents("execution_grant", "grant_json", correlation_id),
            await self._rows("task", correlation_id),
            await self._rows("workflow_run", correlation_id),
            await self._rows("node_run", correlation_id),
            await self._documents("episode", "episode_json", correlation_id),
            await self._documents("dna_execution_context", "context_json", correlation_id),
            await self._documents("audit_record", "record_json", correlation_id),
        )

    async def _documents(self, table: str, column: str, correlation_id: str) -> tuple[Mapping[str, object], ...]:
        rows = await self._database.fetch_all(
            f"SELECT {column} FROM {table} WHERE correlation_id = ? ORDER BY rowid", (correlation_id,)
        )
        return tuple(json.loads(str(row[column])) for row in rows)

    async def _rows(self, table: str, correlation_id: str) -> tuple[Mapping[str, object], ...]:
        rows = await self._database.fetch_all(
            f"SELECT * FROM {table} WHERE correlation_id = ? ORDER BY rowid", (correlation_id,)
        )
        return tuple(dict(row) for row in rows)


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
