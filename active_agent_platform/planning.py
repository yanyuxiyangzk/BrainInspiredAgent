"""Immutable CandidatePlan facts, decisions and ExecutionGrant lifecycle."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from active_agent_platform.storage import SQLiteTransaction


class PlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanDecisionType(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class GrantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    plan_id: str
    document: Mapping[str, object]
    digest: str
    created_at: datetime
    expires_at: datetime
    correlation_id: str

    @classmethod
    def create(cls, document: Mapping[str, object]) -> CandidatePlan:
        if document.get("status") != "CANDIDATE":
            raise PlanningError("PLAN_SCHEMA_INVALID", "plan status must be CANDIDATE")
        plan_id = _text(document, "plan_id")
        correlation_id = _text(document, "correlation_id")
        created_at = _date(document, "created_at")
        expires_at = _date(document, "expires_at")
        if expires_at <= created_at:
            raise PlanningError("PLAN_SCHEMA_INVALID", "plan expiry must follow creation")
        encoded = _canonical(document)
        return cls(
            plan_id,
            cast(Mapping[str, object], _freeze(document)),
            "sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
            created_at,
            expires_at,
            correlation_id,
        )


@dataclass(frozen=True, slots=True)
class PlanDecision:
    decision_id: str
    plan_id: str
    decision: PlanDecisionType
    document: Mapping[str, object]
    decided_at: datetime
    correlation_id: str

    @classmethod
    def create(cls, document: Mapping[str, object]) -> PlanDecision:
        try:
            decision = PlanDecisionType(_text(document, "decision"))
        except ValueError as error:
            raise PlanningError("PLAN_DECISION_INVALID", "unsupported plan decision") from error
        return cls(
            _text(document, "decision_id"),
            _text(document, "plan_id"),
            decision,
            cast(Mapping[str, object], _freeze(document)),
            _date(document, "decided_at"),
            _text(document, "correlation_id"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionGrant:
    grant_id: str
    decision_id: str
    task_id: str
    document: Mapping[str, object]
    status: GrantStatus
    issued_at: datetime
    expires_at: datetime
    correlation_id: str


@dataclass(frozen=True, slots=True)
class GrantAttempt:
    grant_id: str
    task_id: str
    attempt: int
    authorized_at: datetime
    correlation_id: str


class PlanningRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def add_plan(self, plan: CandidatePlan) -> CandidatePlan:
        try:
            await self._transaction.execute(
                "INSERT INTO plan VALUES (?, ?, ?, 'CANDIDATE', ?, ?, ?)",
                (
                    plan.plan_id,
                    _canonical(plan.document),
                    plan.digest,
                    _timestamp(plan.created_at),
                    _timestamp(plan.expires_at),
                    plan.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PlanningError("PLAN_ALREADY_EXISTS", "plan ID or digest already exists") from error
        return plan

    async def get_plan(self, plan_id: str) -> CandidatePlan:
        row = await self._transaction.fetch_one("SELECT * FROM plan WHERE plan_id = ?", (plan_id,))
        if row is None:
            raise PlanningError("PLAN_NOT_FOUND", "candidate plan not found")
        document = _object(str(row["plan_json"]))
        plan = CandidatePlan.create(document)
        if plan.digest != str(row["digest"]):
            raise PlanningError("PLAN_DIGEST_MISMATCH", "stored plan digest does not match content")
        return plan

    async def add_decision(self, decision: PlanDecision) -> PlanDecision:
        plan = await self.get_plan(decision.plan_id)
        if decision.correlation_id != plan.correlation_id:
            raise PlanningError("PLAN_DECISION_INVALID", "decision correlation does not match plan")
        if decision.decided_at >= plan.expires_at and decision.decision is PlanDecisionType.APPROVED:
            raise PlanningError("PLAN_EXPIRED", "expired plan cannot be approved")
        try:
            await self._transaction.execute(
                "INSERT INTO plan_decision VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    decision.plan_id,
                    decision.decision,
                    _canonical(decision.document),
                    _timestamp(decision.decided_at),
                    decision.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PlanningError("PLAN_ALREADY_DECIDED", "plan already has a final decision") from error
        return decision

    async def get_decision(self, decision_id: str) -> PlanDecision:
        row = await self._transaction.fetch_one(
            "SELECT decision_json FROM plan_decision WHERE decision_id = ?", (decision_id,)
        )
        if row is None:
            raise PlanningError("PLAN_DECISION_NOT_FOUND", "plan decision not found")
        return PlanDecision.create(_object(str(row["decision_json"])))


class GrantIssuer:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction
        self._plans = PlanningRepository(transaction)

    async def issue(self, document: Mapping[str, object]) -> ExecutionGrant:
        decision = await self._plans.get_decision(_text(document, "decision_id"))
        if decision.decision is not PlanDecisionType.APPROVED:
            raise PlanningError("GRANT_NOT_ALLOWED", "only an approved decision can produce a grant")
        plan = await self._plans.get_plan(decision.plan_id)
        if _text(document, "plan_id") != plan.plan_id:
            raise PlanningError("GRANT_INVALID", "grant plan does not match decision")
        issued_at, expires_at = _date(document, "issued_at"), _date(document, "expires_at")
        if issued_at >= expires_at or expires_at > plan.expires_at:
            raise PlanningError("GRANT_INVALID", "grant validity must fit within plan validity")
        if document.get("consumption") != "SINGLE_TASK_MULTI_ATTEMPT":
            raise PlanningError("GRANT_INVALID", "unsupported grant consumption mode")
        grant = ExecutionGrant(
            _text(document, "grant_id"),
            decision.decision_id,
            _text(document, "task_id"),
            cast(Mapping[str, object], _freeze(document)),
            GrantStatus.ACTIVE,
            issued_at,
            expires_at,
            _text(document, "correlation_id"),
        )
        if grant.correlation_id != plan.correlation_id:
            raise PlanningError("GRANT_INVALID", "grant correlation does not match plan")
        try:
            await self._transaction.execute(
                "INSERT INTO execution_grant VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
                (
                    grant.grant_id,
                    grant.decision_id,
                    grant.task_id,
                    _canonical(grant.document),
                    _timestamp(grant.issued_at),
                    _timestamp(grant.expires_at),
                    grant.correlation_id,
                ),
            )
            await self._transition(grant, None, GrantStatus.ACTIVE, "grant issued", issued_at)
        except sqlite3.IntegrityError as error:
            raise PlanningError("GRANT_ALREADY_EXISTS", "grant or task already authorized") from error
        return grant

    async def get(self, grant_id: str) -> ExecutionGrant:
        row = await self._transaction.fetch_one(
            "SELECT * FROM execution_grant WHERE grant_id = ?", (grant_id,)
        )
        if row is None:
            raise PlanningError("GRANT_NOT_FOUND", "execution grant not found")
        return ExecutionGrant(
            str(row["grant_id"]),
            str(row["decision_id"]),
            str(row["task_id"]),
            cast(Mapping[str, object], _freeze(_object(str(row["grant_json"])))),
            GrantStatus(str(row["status"])),
            _datetime(row["issued_at"]),
            _datetime(row["expires_at"]),
            str(row["correlation_id"]),
        )

    async def revoke(self, grant_id: str, *, reason: str, occurred_at: datetime) -> ExecutionGrant:
        return await self._end(grant_id, GrantStatus.REVOKED, reason, occurred_at, require_expired=False)

    async def expire(self, grant_id: str, *, occurred_at: datetime) -> ExecutionGrant:
        return await self._end(
            grant_id, GrantStatus.EXPIRED, "grant expired", occurred_at, require_expired=True
        )

    async def authorize_attempt(
        self, grant_id: str, task_id: str, attempt: int, *, authorized_at: datetime
    ) -> GrantAttempt:
        grant = await self.get(grant_id)
        if grant.status is not GrantStatus.ACTIVE:
            raise PlanningError("GRANT_NOT_ACTIVE", "grant is revoked or expired")
        if authorized_at >= grant.expires_at:
            raise PlanningError("GRANT_EXPIRED", "grant expired before task attempt")
        if task_id != grant.task_id:
            raise PlanningError("GRANT_TASK_MISMATCH", "grant cannot authorize another task")
        previous = await self._transaction.fetch_one(
            "SELECT max(attempt) AS attempt FROM grant_attempt WHERE grant_id = ?", (grant_id,)
        )
        expected = 1 if previous is None or previous["attempt"] is None else int(previous["attempt"]) + 1
        if attempt != expected:
            raise PlanningError("TASK_ATTEMPT_INVALID", "task attempts must be sequential")
        await self._transaction.execute(
            "INSERT INTO grant_attempt VALUES (?, ?, ?, ?, ?)",
            (grant_id, task_id, attempt, _timestamp(authorized_at), grant.correlation_id),
        )
        return GrantAttempt(grant_id, task_id, attempt, authorized_at.astimezone(UTC), grant.correlation_id)

    async def _end(
        self,
        grant_id: str,
        status: GrantStatus,
        reason: str,
        occurred_at: datetime,
        *,
        require_expired: bool,
    ) -> ExecutionGrant:
        grant = await self.get(grant_id)
        if grant.status is not GrantStatus.ACTIVE:
            raise PlanningError("GRANT_NOT_ACTIVE", "grant is already terminal")
        if not reason or len(reason) > 500:
            raise PlanningError("GRANT_INVALID", "transition reason must contain 1 to 500 characters")
        if require_expired and occurred_at < grant.expires_at:
            raise PlanningError("GRANT_NOT_EXPIRED", "grant expiry has not arrived")
        await self._transaction.execute(
            "UPDATE execution_grant SET status = ? WHERE grant_id = ? AND status = 'ACTIVE'",
            (status, grant_id),
        )
        await self._transition(grant, GrantStatus.ACTIVE, status, reason, occurred_at)
        return await self.get(grant_id)

    async def _transition(
        self,
        grant: ExecutionGrant,
        previous: GrantStatus | None,
        status: GrantStatus,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        digest = hashlib.sha256(
            f"{grant.grant_id}:{status}:{_timestamp(occurred_at)}".encode()
        ).hexdigest()
        await self._transaction.execute(
            "INSERT INTO execution_grant_transition VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                grant.grant_id,
                previous,
                status,
                reason,
                _timestamp(occurred_at),
                grant.correlation_id,
            ),
        )


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise PlanningError("PLAN_SCHEMA_INVALID", "document must be JSON-compatible")


def _text(value: Mapping[str, object], key: str) -> str:
    found = value.get(key)
    if not isinstance(found, str) or not found or len(found) > 255:
        raise PlanningError("PLAN_SCHEMA_INVALID", f"{key} must be a non-empty string")
    return found


def _date(value: Mapping[str, object], key: str) -> datetime:
    try:
        found = datetime.fromisoformat(_text(value, key))
    except ValueError as error:
        raise PlanningError("PLAN_SCHEMA_INVALID", f"{key} must be an ISO datetime") from error
    if found.tzinfo is None or found.utcoffset() is None:
        raise PlanningError("PLAN_SCHEMA_INVALID", f"{key} must be timezone-aware")
    return found.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlanningError("PLAN_SCHEMA_INVALID", "timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _object(value: str) -> Mapping[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, Mapping):
        raise PlanningError("PLAN_SCHEMA_INVALID", "stored document must be an object")
    return decoded
