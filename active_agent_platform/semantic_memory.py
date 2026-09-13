"""G05 evidence-backed semantic-memory candidates and promotion boundary.

X-05 生命周期治理：矛盾处理（显式裁决，不静默覆盖）、TTL 续期（数据版本
一致且有界）与全生命周期审计（追加只进、哈希链防篡改、可校验）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


class AuditEventType(StrEnum):
    PROPOSED = "PROPOSED"
    PROMOTED = "PROMOTED"
    PROMOTION_REFUSED = "PROMOTION_REFUSED"
    CONFLICT_LINKED = "CONFLICT_LINKED"
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
    RENEWED = "RENEWED"
    EXPIRED = "EXPIRED"


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


@dataclass(frozen=True, slots=True)
class ConflictResolutionResult:
    winner: SemanticMemoryRecord
    rejected_loser_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenewalResult:
    record: SemanticMemoryRecord
    previous_valid_until: datetime
    valid_until: datetime


@dataclass(frozen=True, slots=True)
class TtlPolicy:
    """续期有界策略：created_at → valid_until 的总生命周期不得超过上限。"""

    policy_version: str = "semantic-ttl/1.0"
    maximum_total_lifetime: timedelta = timedelta(days=365)

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise SemanticMemoryError("TTL_POLICY_INVALID", "policy version must not be empty")
        if self.maximum_total_lifetime <= timedelta(0):
            raise SemanticMemoryError("TTL_POLICY_INVALID", "maximum lifetime must be positive")


_AUDIT_GENESIS = "sha256:genesis"


def _audit_digest(
    sequence: int,
    memory_id: str,
    event_type: str,
    reason: str | None,
    detail_json: str,
    occurred_at: str,
    correlation_id: str,
    previous_digest: str,
) -> str:
    payload = json.dumps(
        [sequence, memory_id, event_type, reason, detail_json, occurred_at, correlation_id, previous_digest],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    sequence: int
    memory_id: str
    event_type: AuditEventType
    reason: str | None
    detail_json: str
    occurred_at: datetime
    correlation_id: str
    previous_digest: str
    entry_digest: str

    def to_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "memory_id": self.memory_id,
            "event_type": self.event_type.value,
            "reason": self.reason,
            "detail": json.loads(self.detail_json),
            "occurred_at": _time(self.occurred_at),
            "correlation_id": self.correlation_id,
            "previous_digest": self.previous_digest,
            "entry_digest": self.entry_digest,
        }


@dataclass(frozen=True, slots=True)
class AuditVerification:
    valid: bool
    entries: int
    broken_at: int | None


class SemanticMemoryAuditLog:
    """同一事务内的追加只进审计：哈希链使任何历史改写可被 verify 检出。"""

    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def append(
        self,
        memory_id: str,
        event_type: AuditEventType,
        *,
        occurred_at: datetime,
        correlation_id: str,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> AuditEntry:
        tip = await self._transaction.fetch_one(
            "SELECT sequence, entry_digest FROM semantic_memory_audit ORDER BY sequence DESC LIMIT 1"
        )
        sequence = 1 if tip is None else int(tip["sequence"]) + 1
        previous_digest = _AUDIT_GENESIS if tip is None else str(tip["entry_digest"])
        moment = _time(occurred_at)
        detail_json = _json_value(dict(detail) if detail else {})
        entry_digest = _audit_digest(
            sequence, memory_id, str(event_type), reason, detail_json, moment, correlation_id, previous_digest
        )
        await self._transaction.execute(
            "INSERT INTO semantic_memory_audit"
            " (sequence, memory_id, event_type, reason, detail_json, occurred_at, correlation_id, previous_digest, entry_digest)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence, memory_id, str(event_type), reason, detail_json, moment,
                correlation_id, previous_digest, entry_digest,
            ),
        )
        return AuditEntry(
            sequence, memory_id, event_type, reason, detail_json,
            _utc(occurred_at), correlation_id, previous_digest, entry_digest,
        )

    async def entries(self, *, memory_id: str | None = None) -> tuple[AuditEntry, ...]:
        if memory_id is None:
            rows = await self._transaction.fetch_all(
                "SELECT * FROM semantic_memory_audit ORDER BY sequence"
            )
        else:
            rows = await self._transaction.fetch_all(
                "SELECT * FROM semantic_memory_audit WHERE memory_id = ? ORDER BY sequence",
                (memory_id,),
            )
        return tuple(_audit_entry(row) for row in rows)

    async def verify(self) -> AuditVerification:
        rows = await self._transaction.fetch_all(
            "SELECT * FROM semantic_memory_audit ORDER BY sequence"
        )
        expected_sequence = 1
        expected_previous = _AUDIT_GENESIS
        for row in rows:
            sequence = int(row["sequence"])
            stored_digest = str(row["entry_digest"])
            recomputed = _audit_digest(
                sequence,
                str(row["memory_id"]),
                str(row["event_type"]),
                None if row["reason"] is None else str(row["reason"]),
                str(row["detail_json"]),
                str(row["occurred_at"]),
                str(row["correlation_id"]),
                str(row["previous_digest"]),
            )
            if sequence != expected_sequence or str(row["previous_digest"]) != expected_previous or stored_digest != recomputed:
                return AuditVerification(False, len(rows), sequence)
            expected_sequence += 1
            expected_previous = stored_digest
        return AuditVerification(True, len(rows), None)


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
        audit = SemanticMemoryAuditLog(self._transaction)
        await audit.append(
            record.memory_id, AuditEventType.PROPOSED,
            occurred_at=record.created_at, correlation_id=record.candidate.correlation_id,
        )
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
            await audit.append(
                conflict_id, AuditEventType.CONFLICT_LINKED,
                occurred_at=record.created_at, correlation_id=record.candidate.correlation_id,
                detail={"linked": record.memory_id, "role": "peer"},
            )
        if conflict_ids:
            await audit.append(
                record.memory_id, AuditEventType.CONFLICT_LINKED,
                occurred_at=record.created_at, correlation_id=record.candidate.correlation_id,
                detail={"linked": list(conflict_ids), "role": "record"},
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
        audit = SemanticMemoryAuditLog(self._transaction)
        moment = _utc(now)
        if record.status is not SemanticStatus.CANDIDATE:
            reason = "only a candidate can be promoted"
            await audit.append(
                memory_id, AuditEventType.PROMOTION_REFUSED,
                occurred_at=moment, correlation_id=record.candidate.correlation_id, reason=reason,
            )
            return PromotionResult(False, record, reason)
        if record.candidate.valid_until <= moment:
            await self._set_status(record.memory_id, SemanticStatus.EXPIRED, moment)
            reason = "candidate has expired"
            await audit.append(
                memory_id, AuditEventType.EXPIRED,
                occurred_at=moment, correlation_id=record.candidate.correlation_id,
                detail={"via": "promotion"},
            )
            await audit.append(
                memory_id, AuditEventType.PROMOTION_REFUSED,
                occurred_at=moment, correlation_id=record.candidate.correlation_id, reason=reason,
            )
            return PromotionResult(False, await self.get(memory_id), reason)
        if record.contradicted_by:
            reason = "candidate has unresolved contradictions"
            await audit.append(
                memory_id, AuditEventType.PROMOTION_REFUSED,
                occurred_at=moment, correlation_id=record.candidate.correlation_id, reason=reason,
            )
            return PromotionResult(False, record, reason)
        await self._require_evidence(record.candidate.evidence_episode_ids)
        await self._transaction.execute(
            "UPDATE semantic_memory SET status = 'VALIDATED', validation_method = ?, updated_at = ? WHERE memory_id = ? AND status = 'CANDIDATE'",
            (validation_method, _time(moment), memory_id),
        )
        await audit.append(
            memory_id, AuditEventType.PROMOTED,
            occurred_at=moment, correlation_id=record.candidate.correlation_id,
            detail={"validation_method": validation_method},
        )
        return PromotionResult(True, await self.get(memory_id), "candidate validated")

    async def expire_due(self, *, now: datetime) -> tuple[str, ...]:
        rows = await self._transaction.fetch_all(
            "SELECT memory_id, correlation_id FROM semantic_memory WHERE status IN ('CANDIDATE', 'VALIDATED') AND valid_until <= ? ORDER BY memory_id",
            (_time(now),),
        )
        ids = tuple(str(row["memory_id"]) for row in rows)
        if ids:
            await self._transaction.execute(
                "UPDATE semantic_memory SET status = 'EXPIRED', updated_at = ? WHERE status IN ('CANDIDATE', 'VALIDATED') AND valid_until <= ?",
                (_time(now), _time(now)),
            )
            audit = SemanticMemoryAuditLog(self._transaction)
            for row in rows:
                await audit.append(
                    str(row["memory_id"]), AuditEventType.EXPIRED,
                    occurred_at=now, correlation_id=str(row["correlation_id"]),
                )
        return ids

    async def resolve_conflict(
        self, winner_id: str, loser_ids: tuple[str, ...], *, method: str, now: datetime,
    ) -> ConflictResolutionResult:
        """显式裁决矛盾组：losers 转 REJECTED 终态，winner 解除矛盾链接。

        全部校验先行，任一参与方不合法即整体拒绝，不落任何写。
        """
        moment = _utc(now)
        if not method:
            raise SemanticMemoryError("RESOLUTION_METHOD_REQUIRED", "conflict resolution requires a method")
        if not loser_ids or len(set(loser_ids)) != len(loser_ids) or winner_id in loser_ids:
            raise SemanticMemoryError(
                "CONFLICT_RESOLUTION_INVALID", "loser ids must be unique and disjoint from the winner"
            )
        winner = await self.get(winner_id)
        if winner.status not in (SemanticStatus.CANDIDATE, SemanticStatus.VALIDATED):
            raise SemanticMemoryError("CONFLICT_RESOLUTION_INVALID", "winner is in a terminal status")
        if winner.candidate.valid_until <= moment:
            raise SemanticMemoryError(
                "CONFLICT_RESOLUTION_INVALID", "winner has expired; run expire_due first"
            )
        losers: list[SemanticMemoryRecord] = []
        for loser_id in loser_ids:
            loser = await self.get(loser_id)
            if loser.status not in (SemanticStatus.CANDIDATE, SemanticStatus.VALIDATED):
                raise SemanticMemoryError("CONFLICT_RESOLUTION_INVALID", "loser is in a terminal status")
            if loser.candidate.valid_until <= moment:
                raise SemanticMemoryError("CONFLICT_RESOLUTION_INVALID", "loser has expired")
            if winner_id not in loser.contradicted_by or loser_id not in winner.contradicted_by:
                raise SemanticMemoryError(
                    "CONFLICT_RESOLUTION_INVALID", "participants do not form a contradiction link"
                )
            losers.append(loser)
        audit = SemanticMemoryAuditLog(self._transaction)
        for loser in losers:
            await self._transaction.execute(
                "UPDATE semantic_memory SET status = 'REJECTED', updated_at = ? WHERE memory_id = ?",
                (_time(moment), loser.memory_id),
            )
            await audit.append(
                loser.memory_id, AuditEventType.CONFLICT_RESOLVED,
                occurred_at=moment, correlation_id=loser.candidate.correlation_id,
                detail={"method": method, "role": "loser", "winner": winner_id},
            )
        resolved = set(loser_ids)
        await self._transaction.execute(
            "UPDATE semantic_memory SET contradicted_by_json = ?, updated_at = ? WHERE memory_id = ?",
            (
                _json_value([item for item in winner.contradicted_by if item not in resolved]),
                _time(moment), winner_id,
            ),
        )
        await audit.append(
            winner_id, AuditEventType.CONFLICT_RESOLVED,
            occurred_at=moment, correlation_id=winner.candidate.correlation_id,
            detail={"method": method, "role": "winner", "losers": sorted(loser_ids)},
        )
        return ConflictResolutionResult(await self.get(winner_id), tuple(loser_ids))

    async def renew(
        self, memory_id: str, *, data_version: str, ttl: timedelta, now: datetime, policy: TtlPolicy,
    ) -> RenewalResult:
        """TTL 续期：仅 VALIDATED、数据版本一致且总生命周期在策略界内可延长。"""
        if ttl <= timedelta(0):
            raise SemanticMemoryError("RENEWAL_TTL_INVALID", "renewal ttl must be positive")
        record = await self.get(memory_id)
        if record.status is not SemanticStatus.VALIDATED:
            raise SemanticMemoryError("RENEWAL_INVALID_STATE", "only a validated memory can be renewed")
        if record.candidate.data_version != data_version:
            raise SemanticMemoryError(
                "RENEWAL_VERSION_MISMATCH", "renewal data version does not match the stored memory"
            )
        moment = _utc(now)
        extended = max(moment, record.candidate.valid_until) + ttl
        if extended - record.created_at > policy.maximum_total_lifetime:
            raise SemanticMemoryError(
                "RENEWAL_LIFETIME_EXCEEDED", "renewal exceeds the maximum total lifetime"
            )
        await self._transaction.execute(
            "UPDATE semantic_memory SET valid_until = ?, updated_at = ? WHERE memory_id = ?",
            (_time(extended), _time(moment), memory_id),
        )
        await SemanticMemoryAuditLog(self._transaction).append(
            memory_id, AuditEventType.RENEWED,
            occurred_at=moment, correlation_id=record.candidate.correlation_id,
            detail={
                "policy": policy.policy_version,
                "previous_valid_until": _time(record.candidate.valid_until),
                "valid_until": _time(extended),
            },
        )
        return RenewalResult(await self.get(memory_id), record.candidate.valid_until, extended)

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
    def __init__(
        self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator,
        *, ttl_policy: TtlPolicy | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._identifiers = identifiers
        self._ttl_policy = ttl_policy if ttl_policy is not None else TtlPolicy()

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

    async def resolve_conflict(
        self, winner_id: str, loser_ids: tuple[str, ...], *, method: str,
    ) -> ConflictResolutionResult:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).resolve_conflict(
                winner_id, loser_ids, method=method, now=self._clock.now()
            )

    async def renew(self, memory_id: str, *, data_version: str, ttl: timedelta) -> RenewalResult:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryRepository(transaction).renew(
                memory_id, data_version=data_version, ttl=ttl,
                now=self._clock.now(), policy=self._ttl_policy,
            )

    async def audit(self, memory_id: str | None = None) -> tuple[AuditEntry, ...]:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryAuditLog(transaction).entries(memory_id=memory_id)

    async def verify_audit(self) -> AuditVerification:
        async with self._database.transaction() as transaction:
            return await SemanticMemoryAuditLog(transaction).verify()


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


def _audit_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        int(row["sequence"]), str(row["memory_id"]), AuditEventType(str(row["event_type"])),
        None if row["reason"] is None else str(row["reason"]), str(row["detail_json"]),
        _datetime(row["occurred_at"]), str(row["correlation_id"]),
        str(row["previous_digest"]), str(row["entry_digest"]),
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
