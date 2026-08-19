"""G05 evidence-backed semantic-memory candidates and promotion boundary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator


class SemanticMemoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SemanticStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    claim_key: str
    claim_value: object
    statement: str
    summary: str
    evidence_episode_ids: tuple[str, ...]
    scope: Mapping[str, object]
    conditions: Mapping[str, object]
    confidence: float
    data_version: str
    valid_until: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if not all((self.claim_key, self.statement, self.summary, self.data_version, self.correlation_id)):
            raise ValueError("semantic candidate text fields must not be empty")
        if not self.evidence_episode_ids or len(set(self.evidence_episode_ids)) != len(self.evidence_episode_ids):
            raise ValueError("semantic candidate requires unique evidence Episodes")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "valid_until", _utc(self.valid_until))
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))


@dataclass(frozen=True, slots=True)
class SemanticMemoryRecord:
    memory_id: str
    candidate: SemanticCandidate
    status: SemanticStatus
    validation_method: str | None
    contradicted_by: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PromotionResult:
    promoted: bool
    record: SemanticMemoryRecord
    reason: str


class SemanticMemoryRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def add(self, record: SemanticMemoryRecord) -> SemanticMemoryRecord:
        await self._require_evidence(record.candidate.evidence_episode_ids)
        scope_digest = _digest(record.candidate.scope)
        conflicts = await self._transaction.fetch_all(
            "SELECT memory_id, claim_value_json, contradicted_by_json FROM semantic_memory WHERE claim_key = ? AND scope_digest = ? AND status IN ('CANDIDATE', 'VALIDATED')",
            (record.candidate.claim_key, scope_digest),
        )
        conflict_ids = tuple(
            str(row["memory_id"])
            for row in conflicts
            if str(row["claim_value_json"]) != _json_value(record.candidate.claim_value)
        )
        record = SemanticMemoryRecord(
            record.memory_id, record.candidate, record.status, record.validation_method,
            tuple(sorted(set(record.contradicted_by) | set(conflict_ids))), record.created_at, record.updated_at,
        )
        try:
            await self._transaction.execute(
                "INSERT INTO semantic_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.memory_id, record.candidate.claim_key, _json_value(record.candidate.claim_value),
                    record.candidate.statement, record.candidate.summary, _json(record.candidate.scope), scope_digest,
                    _json(record.candidate.conditions), record.candidate.confidence, record.validation_method,
                    record.candidate.data_version, _json_value(list(record.candidate.evidence_episode_ids)),
                    _time(record.candidate.valid_until), record.status, _json_value(list(record.contradicted_by)),
                    _time(record.created_at), _time(record.updated_at), record.candidate.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise SemanticMemoryError("SEMANTIC_CANDIDATE_DUPLICATE", "semantic candidate already exists") from error
        for row in conflicts:
            conflict_id = str(row["memory_id"])
            if conflict_id not in conflict_ids:
                continue
            links = set(json.loads(str(row["contradicted_by_json"])))
            links.add(record.memory_id)
            await self._transaction.execute(
                "UPDATE semantic_memory SET contradicted_by_json = ?, updated_at = ? WHERE memory_id = ?",
                (_json_value(sorted(links)), _time(record.created_at), conflict_id),
            )
        return record

    async def get(self, memory_id: str) -> SemanticMemoryRecord:
        row = await self._transaction.fetch_one("SELECT * FROM semantic_memory WHERE memory_id = ?", (memory_id,))
        if row is None:
            raise SemanticMemoryError("SEMANTIC_MEMORY_NOT_FOUND", "semantic memory does not exist")
        return _record(row)

    async def promote(self, memory_id: str, *, validation_method: str, now: datetime) -> PromotionResult:
        if not validation_method:
            raise SemanticMemoryError("VALIDATION_METHOD_REQUIRED", "promotion requires a validation method")
        record = await self.get(memory_id)
        moment = _utc(now)
        if record.status is not SemanticStatus.CANDIDATE:
            return PromotionResult(False, record, "only a candidate can be promoted")
        if record.candidate.valid_until <= moment:
            await self._set_status(record.memory_id, SemanticStatus.EXPIRED, moment)
            return PromotionResult(False, await self.get(memory_id), "candidate has expired")
        if record.contradicted_by:
            return PromotionResult(False, record, "candidate has unresolved contradictions")
        await self._require_evidence(record.candidate.evidence_episode_ids)
        await self._transaction.execute(
            "UPDATE semantic_memory SET status = 'VALIDATED', validation_method = ?, updated_at = ? WHERE memory_id = ? AND status = 'CANDIDATE'",
            (validation_method, _time(moment), memory_id),
        )
        return PromotionResult(True, await self.get(memory_id), "candidate validated")

    async def expire_due(self, *, now: datetime) -> tuple[str, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT memory_id FROM semantic_memory WHERE status IN ('CANDIDATE', 'VALIDATED') AND valid_until <= ? ORDER BY memory_id",
            (_time(now),),
        )
        ids = tuple(str(row["memory_id"]) for row in rows)
        if ids:
            await self._transaction.execute(
                "UPDATE semantic_memory SET status = 'EXPIRED', updated_at = ? WHERE status IN ('CANDIDATE', 'VALIDATED') AND valid_until <= ?",
                (_time(now), _time(now)),
            )
        return ids

    async def validated(self, *, now: datetime) -> tuple[SemanticMemoryRecord, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT * FROM semantic_memory WHERE status = 'VALIDATED' AND valid_until > ? ORDER BY confidence DESC, created_at DESC, memory_id",
            (_time(now),),
        )
        return tuple(_record(row) for row in rows)

    async def _require_evidence(self, ids: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in ids)
        rows = await self._transaction.fetch_all(
            f"SELECT episode_id FROM episode WHERE episode_id IN ({placeholders})", ids
        )
        if {str(row["episode_id"]) for row in rows} != set(ids):
            raise SemanticMemoryError("SEMANTIC_EVIDENCE_MISSING", "candidate evidence Episode is missing")

    async def _set_status(self, memory_id: str, status: SemanticStatus, now: datetime) -> None:
        await self._transaction.execute(
            "UPDATE semantic_memory SET status = ?, updated_at = ? WHERE memory_id = ?",
            (status, _time(now), memory_id),
        )


class SemanticMemoryService:
    def __init__(self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator) -> None:
        self._database = database
        self._clock = clock
        self._identifiers = identifiers

    async def propose(self, candidate: SemanticCandidate) -> SemanticMemoryRecord:
        now = _utc(self._clock.now())
        if candidate.valid_until <= now:
            raise SemanticMemoryError("SEMANTIC_CANDIDATE_EXPIRED", "candidate is already expired")
        record = SemanticMemoryRecord(
            str(self._identifiers.new()), candidate, SemanticStatus.CANDIDATE, None, (), now, now,
        )
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).add(record)

    async def promote(self, memory_id: str, *, validation_method: str) -> PromotionResult:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).promote(
                memory_id, validation_method=validation_method, now=self._clock.now()
            )

    async def expire_due(self) -> tuple[str, ...]:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).expire_due(now=self._clock.now())

    async def validated(self) -> tuple[SemanticMemoryRecord, ...]:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).validated(now=self._clock.now())


def _record(row: sqlite3.Row) -> SemanticMemoryRecord:
    candidate = SemanticCandidate(
        str(row["claim_key"]), json.loads(str(row["claim_value_json"])), str(row["statement"]),
        str(row["summary"]), tuple(json.loads(str(row["evidence_json"]))),
        json.loads(str(row["scope_json"])), json.loads(str(row["conditions_json"])),
        float(row["confidence"]), str(row["data_version"]), _datetime(row["valid_until"]),
        str(row["correlation_id"]),
    )
    return SemanticMemoryRecord(
        str(row["memory_id"]), candidate, SemanticStatus(str(row["status"])),
        None if row["validation_method"] is None else str(row["validation_method"]),
        tuple(json.loads(str(row["contradicted_by_json"]))), _datetime(row["created_at"]),
        _datetime(row["updated_at"]),
    )


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))


def _json_value(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SemanticMemoryError("TIME_INVALID", "semantic memory time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value))
