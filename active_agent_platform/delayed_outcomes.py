"""G03 persistent delayed-evaluation windows and append-only evidence ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from active_agent_platform.outcomes import (
    OutcomeError,
    OutcomeEvaluation,
    OutcomeEvaluator,
    OutcomeRequest,
)
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator


class WindowStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    EVALUATED = "EVALUATED"


@dataclass(frozen=True, slots=True)
class DelayedEvaluationWindow:
    window_id: str
    task_id: str
    episode_id: str | None
    opens_at: datetime
    closes_at: datetime
    status: WindowStatus
    evaluator_version: str
    created_at: datetime
    correlation_id: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    ledger_id: str
    window_id: str
    evidence_id: str
    evidence_type: str
    evidence: Mapping[str, object]
    digest: str
    observed_at: datetime
    created_at: datetime
    correlation_id: str


class DelayedOutcomeError(OutcomeError):
    pass


class DelayedOutcomeRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def create_window(self, window: DelayedEvaluationWindow) -> None:
        if window.closes_at <= window.opens_at:
            raise DelayedOutcomeError("WINDOW_INVALID", "window close must follow open")
        try:
            await self._transaction.execute(
                "INSERT INTO delayed_evaluation_window VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    window.window_id, window.task_id, window.episode_id,
                    _time(window.opens_at), _time(window.closes_at), window.status,
                    window.evaluator_version, _time(window.created_at), window.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise DelayedOutcomeError("WINDOW_ALREADY_EXISTS", "evaluation window already exists") from error

    async def get_window(self, window_id: str) -> DelayedEvaluationWindow:
        row = await self._transaction.fetch_one(
            "SELECT * FROM delayed_evaluation_window WHERE window_id = ?", (window_id,)
        )
        if row is None:
            raise DelayedOutcomeError("WINDOW_NOT_FOUND", "evaluation window not found")
        return _window(row)

    async def append_evidence(self, entry: LedgerEntry, *, now: datetime) -> None:
        window = await self.get_window(entry.window_id)
        if window.status is not WindowStatus.OPEN:
            raise DelayedOutcomeError("WINDOW_CLOSED", "closed or expired window cannot accept evidence")
        if _utc(now) < window.opens_at:
            raise DelayedOutcomeError("WINDOW_NOT_OPEN", "evaluation window has not opened")
        if _utc(now) >= window.closes_at:
            raise DelayedOutcomeError("WINDOW_EXPIRED", "evaluation window has expired")
        if entry.correlation_id != window.correlation_id:
            raise DelayedOutcomeError("EVIDENCE_CONTEXT_MISMATCH", "evidence correlation differs from window")
        observed = _utc(entry.observed_at)
        if observed < window.opens_at or observed >= window.closes_at or observed > _utc(now):
            raise DelayedOutcomeError("EVIDENCE_TIME_INVALID", "evidence time is outside the observed window")
        if not entry.evidence_id or not entry.evidence_type:
            raise DelayedOutcomeError("EVIDENCE_INVALID", "evidence ID and type are required")
        try:
            await self._transaction.execute(
                "INSERT INTO evidence_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.ledger_id, entry.window_id, entry.evidence_id, entry.evidence_type,
                    _json(entry.evidence), entry.digest, _time(entry.observed_at),
                    _time(entry.created_at), entry.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise DelayedOutcomeError("EVIDENCE_ALREADY_EXISTS", "evidence ID already exists in window") from error

    async def close_window(self, window_id: str, *, now: datetime) -> DelayedEvaluationWindow:
        window = await self.get_window(window_id)
        moment = _utc(now)
        if window.status is not WindowStatus.OPEN:
            return window
        if moment < window.closes_at:
            raise DelayedOutcomeError("WINDOW_NOT_DUE", "evaluation window is still open")
        await self._set_status(window, WindowStatus.CLOSED)
        return await self.get_window(window_id)

    async def due_windows(self, *, now: datetime) -> tuple[DelayedEvaluationWindow, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT * FROM delayed_evaluation_window WHERE status = 'OPEN' AND closes_at <= ? ORDER BY closes_at, window_id",
            (_time(now),),
        )
        return tuple(_window(row) for row in rows)

    async def evidence(self, window_id: str) -> tuple[LedgerEntry, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT * FROM evidence_ledger WHERE window_id = ? ORDER BY observed_at, ledger_id", (window_id,)
        )
        return tuple(_entry(row) for row in rows)

    async def _set_status(self, window: DelayedEvaluationWindow, status: WindowStatus) -> None:
        await self._transaction.execute(
            "UPDATE delayed_evaluation_window SET status = ? WHERE window_id = ? AND status = 'OPEN'",
            (status, window.window_id),
        )


class DelayedOutcomeService:
    def __init__(self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator) -> None:
        self._database = database
        self._clock = clock
        self._identifiers = identifiers

    async def open_window(
        self, *, task_id: str, episode_id: str | None, opens_at: datetime, closes_at: datetime,
        evaluator_version: str, correlation_id: str,
    ) -> DelayedEvaluationWindow:
        now = _utc(self._clock.now())
        window = DelayedEvaluationWindow(
            str(self._identifiers.new()), task_id, episode_id, _utc(opens_at), _utc(closes_at),
            WindowStatus.OPEN, evaluator_version, now, correlation_id,
        )
        async with self._database.transaction() as transaction:
            await DelayedOutcomeRepository(transaction).create_window(window)
        return window

    async def append(
        self, window_id: str, *, evidence_id: str, evidence_type: str, evidence: Mapping[str, object],
        observed_at: datetime, correlation_id: str,
    ) -> LedgerEntry:
        now = _utc(self._clock.now())
        digest = "sha256:" + hashlib.sha256(_json(evidence).encode()).hexdigest()
        entry = LedgerEntry(
            str(self._identifiers.new()), window_id, evidence_id, evidence_type,
            dict(evidence), digest, _utc(observed_at), now, correlation_id,
        )
        async with self._database.transaction() as transaction:
            await DelayedOutcomeRepository(transaction).append_evidence(entry, now=now)
        return entry

    async def close(self, window_id: str) -> DelayedEvaluationWindow:
        async with self._database.transaction() as transaction:
            return await DelayedOutcomeRepository(transaction).close_window(window_id, now=self._clock.now())

    async def evaluate(
        self, window_id: str, *, evaluator: OutcomeEvaluator, request: OutcomeRequest,
    ) -> OutcomeEvaluation:
        async with self._database.transaction() as transaction:
            repository = DelayedOutcomeRepository(transaction)
            window = await repository.get_window(window_id)
            if window.status is not WindowStatus.CLOSED:
                raise DelayedOutcomeError("WINDOW_NOT_CLOSED", "window must be closed before delayed evaluation")
            if evaluator.version != window.evaluator_version:
                raise DelayedOutcomeError("EVALUATOR_VERSION_MISMATCH", "evaluation version is not pinned to window")
            if request.task_id != window.task_id or request.correlation_id != window.correlation_id:
                raise DelayedOutcomeError("WINDOW_CONTEXT_MISMATCH", "evaluation request differs from window context")
            entries = await repository.evidence(window_id)
            if tuple(entry.evidence_id for entry in entries) != request.evidence_ids:
                raise DelayedOutcomeError("EVIDENCE_LEDGER_MISMATCH", "evaluation evidence differs from ledger")
        evaluation = await evaluator.evaluate_and_record(request)
        async with self._database.transaction() as transaction:
            window = await DelayedOutcomeRepository(transaction).get_window(window_id)
            if window.status is not WindowStatus.CLOSED:
                raise DelayedOutcomeError("WINDOW_ALREADY_EVALUATED", "window evaluation is not repeatable")
            await transaction.execute(
                "UPDATE delayed_evaluation_window SET status = 'EVALUATED' WHERE window_id = ?",
                (window_id,),
            )
        return evaluation


def _window(row: sqlite3.Row) -> DelayedEvaluationWindow:
    return DelayedEvaluationWindow(
        str(row["window_id"]), str(row["task_id"]), None if row["episode_id"] is None else str(row["episode_id"]),
        _datetime(row["opens_at"]), _datetime(row["closes_at"]), WindowStatus(str(row["status"])),
        str(row["evaluator_version"]), _datetime(row["created_at"]), str(row["correlation_id"]),
    )


def _entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        str(row["ledger_id"]), str(row["window_id"]), str(row["evidence_id"]), str(row["evidence_type"]),
        json.loads(str(row["evidence_json"])), str(row["evidence_digest"]), _datetime(row["observed_at"]),
        _datetime(row["created_at"]), str(row["correlation_id"]),
    )


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DelayedOutcomeError("TIME_INVALID", "timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value))
