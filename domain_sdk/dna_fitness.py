"""Persistent, attributable multi-dimensional fitness projections for DNA versions."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from active_agent_platform.outcomes import OutcomeEvaluation
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.dna import DnaDefinition


class DnaFitnessError(ValueError):
    pass


class FitnessReadiness(StrEnum):
    COLLECTING = "COLLECTING"
    OBSERVING = "OBSERVING"
    READY = "READY"
    RISK_BLOCKED = "RISK_BLOCKED"


@dataclass(frozen=True, slots=True)
class DnaFitnessPolicy:
    policy_version: str
    window_id: str
    starts_at: datetime
    ends_at: datetime
    minimum_samples: int = 30
    maximum_risk_rate: float = 0.0
    confidence_z: float = 1.96

    def __post_init__(self) -> None:
        if not self.policy_version or not self.window_id:
            raise DnaFitnessError("fitness policy identifiers must not be empty")
        start, end = _utc(self.starts_at), _utc(self.ends_at)
        if start >= end:
            raise DnaFitnessError("fitness window must have positive duration")
        if self.minimum_samples < 1:
            raise DnaFitnessError("minimum_samples must be positive")
        if not 0 <= self.maximum_risk_rate <= 1 or self.confidence_z <= 0:
            raise DnaFitnessError("fitness risk and confidence limits are invalid")


@dataclass(frozen=True, slots=True)
class DnaFitnessObservation:
    dna_id: str
    version: str
    content_digest: str
    outcome: OutcomeEvaluation
    cost_minor: int
    latency_ms: int
    stable: bool
    risk_violations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.dna_id or not self.version or not self.content_digest.startswith("sha256:"):
            raise DnaFitnessError("DNA fitness identity is invalid")
        if self.cost_minor < 0 or self.latency_ms < 0:
            raise DnaFitnessError("fitness cost and latency must be non-negative")
        if len(set(self.risk_violations)) != len(self.risk_violations):
            raise DnaFitnessError("risk violations must be unique")


@dataclass(frozen=True, slots=True)
class DnaFitnessSnapshot:
    dna_id: str
    version: str
    content_digest: str
    window_id: str
    policy_version: str
    sample_count: int
    success_rate: float
    success_confidence_lower: float
    evidence_score: float
    user_value_score: float
    average_cost_minor: float
    average_latency_ms: float
    p95_latency_ms: int
    stability_rate: float
    risk_rate: float
    readiness: FitnessReadiness
    projected_at: datetime
    revision: int


class DnaFitnessProjector:
    def __init__(
        self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator,
        policy: DnaFitnessPolicy,
    ) -> None:
        self._database = database
        self._clock = clock
        self._identifiers = identifiers
        self._policy = policy

    async def project(self, observation: DnaFitnessObservation) -> DnaFitnessSnapshot:
        observed_at = _utc(observation.outcome.evaluated_at)
        if not _utc(self._policy.starts_at) <= observed_at < _utc(self._policy.ends_at):
            raise DnaFitnessError("outcome is outside the fitness window")
        payload = _payload(observation, self._policy.window_id)
        payload_digest = _digest(payload)
        async with self._database.transaction() as transaction:
            await self._validate_attribution(transaction, observation)
            cursor = await transaction.execute(
                """INSERT OR IGNORE INTO dna_fitness_observation VALUES (
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (str(self._identifiers.new()), observation.outcome.evaluation_id,
                 observation.outcome.task_id, observation.dna_id, observation.version,
                 observation.content_digest,
                 self._policy.window_id, int(observation.outcome.successful),
                 _score(observation.outcome.evidence.score),
                 _score(observation.outcome.quality.score), observation.cost_minor,
                 observation.latency_ms, int(observation.stable),
                 _json(list(observation.risk_violations)), _time(observed_at), payload_digest,
                 observation.outcome.correlation_id),
            )
            if cursor.rowcount == 0:
                existing = await transaction.fetch_one(
                    "SELECT payload_digest FROM dna_fitness_observation WHERE evaluation_id=?",
                    (observation.outcome.evaluation_id,),
                )
                if existing is None or str(existing["payload_digest"]) != payload_digest:
                    raise DnaFitnessError("outcome already has a different DNA attribution")
                return await self._get(transaction, observation.dna_id, observation.version)
            return await self._rebuild(transaction, observation.dna_id, observation.version,
                                       observation.content_digest)

    async def get(self, dna_id: str, version: str) -> DnaFitnessSnapshot:
        async with self._database.transaction() as transaction:
            return await self._get(transaction, dna_id, version)

    async def refresh(self, dna_id: str, version: str) -> DnaFitnessSnapshot:
        """Re-project readiness after a window closes without requiring a new observation."""
        async with self._database.transaction() as transaction:
            current = await self._get(transaction, dna_id, version)
            return await self._rebuild(transaction, dna_id, version, current.content_digest)

    async def observations(self, dna_id: str, version: str) -> tuple[dict[str, object], ...]:
        rows = await self._database.fetch_all(
            """SELECT * FROM dna_fitness_observation
               WHERE dna_id=? AND version=? AND window_id=? ORDER BY observed_at,evaluation_id""",
            (dna_id, version, self._policy.window_id),
        )
        return tuple(dict(row) for row in rows)

    async def _validate_attribution(
        self, transaction: SQLiteTransaction, observation: DnaFitnessObservation,
    ) -> None:
        dna = await transaction.fetch_one(
            """SELECT content_digest,document_json FROM dna_definition
               WHERE dna_id=? AND version=?""",
            (observation.dna_id, observation.version),
        )
        if dna is None:
            raise DnaFitnessError("attributed DNA version is not registered")
        if str(dna["content_digest"]) != observation.content_digest:
            raise DnaFitnessError("attributed DNA content digest does not match")
        definition = DnaDefinition.from_document(json.loads(str(dna["document_json"])))
        outcome = await transaction.fetch_one(
            """SELECT task_id,correlation_id,evaluation_json FROM outcome_evaluation
               WHERE evaluation_id=?""",
            (observation.outcome.evaluation_id,),
        )
        if outcome is None:
            raise DnaFitnessError("attributed outcome is not persisted")
        if str(outcome["correlation_id"]) != observation.outcome.correlation_id:
            raise DnaFitnessError("outcome correlation does not match")
        if str(outcome["task_id"]) != observation.outcome.task_id:
            raise DnaFitnessError("outcome task does not match")
        if json.loads(str(outcome["evaluation_json"])) != observation.outcome.to_dict():
            raise DnaFitnessError("outcome document does not match persisted evaluation")
        run = await transaction.fetch_one(
            """SELECT run_id FROM workflow_run
               WHERE task_id=? AND workflow_id=? AND workflow_version=? AND workflow_digest=?
               ORDER BY created_at LIMIT 1""",
            (observation.outcome.task_id, definition.workflow_validation.workflow_id,
             definition.version, definition.workflow_validation.digest),
        )
        if run is None:
            raise DnaFitnessError("outcome task has no matching DNA workflow run")

    async def _rebuild(
        self, transaction: SQLiteTransaction, dna_id: str, version: str,
        content_digest: str,
    ) -> DnaFitnessSnapshot:
        rows = await transaction.fetch_all(
            """SELECT * FROM dna_fitness_observation
               WHERE dna_id=? AND version=? AND window_id=? ORDER BY observed_at,evaluation_id""",
            (dna_id, version, self._policy.window_id),
        )
        count = len(rows)
        successes = sum(int(row["successful"]) for row in rows)
        risk_count = sum(bool(json.loads(str(row["risk_violations_json"]))) for row in rows)
        latencies = sorted(int(row["latency_ms"]) for row in rows)
        success_rate = successes / count
        risk_rate = risk_count / count
        now = _utc(self._clock.now())
        readiness = _readiness(count, risk_rate, now, self._policy)
        current = await transaction.fetch_one(
            """SELECT revision FROM dna_fitness_snapshot
               WHERE dna_id=? AND version=? AND window_id=?""",
            (dna_id, version, self._policy.window_id),
        )
        revision = 1 if current is None else int(current["revision"]) + 1
        snapshot = DnaFitnessSnapshot(
            dna_id, version, content_digest, self._policy.window_id,
            self._policy.policy_version, count, _round(success_rate),
            _round(_wilson_lower(successes, count, self._policy.confidence_z)),
            _mean(rows, "evidence_score"), _mean(rows, "user_value_score"),
            _mean(rows, "cost_minor"), _mean(rows, "latency_ms"),
            latencies[math.ceil(0.95 * count) - 1], _mean(rows, "stable"),
            _round(risk_rate), readiness, now, revision,
        )
        await transaction.execute(
            """INSERT INTO dna_fitness_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dna_id,version,window_id) DO UPDATE SET
                 content_digest=excluded.content_digest, policy_version=excluded.policy_version,
                 sample_count=excluded.sample_count, success_rate=excluded.success_rate,
                 success_confidence_lower=excluded.success_confidence_lower,
                 evidence_score=excluded.evidence_score,
                 user_value_score=excluded.user_value_score,
                 average_cost_minor=excluded.average_cost_minor,
                 average_latency_ms=excluded.average_latency_ms,
                 p95_latency_ms=excluded.p95_latency_ms,
                 stability_rate=excluded.stability_rate, risk_rate=excluded.risk_rate,
                 readiness=excluded.readiness, projected_at=excluded.projected_at,
                 revision=excluded.revision""",
            _snapshot_values(snapshot),
        )
        return snapshot

    async def _get(
        self, transaction: SQLiteTransaction, dna_id: str, version: str,
    ) -> DnaFitnessSnapshot:
        row = await transaction.fetch_one(
            """SELECT * FROM dna_fitness_snapshot
               WHERE dna_id=? AND version=? AND window_id=?""",
            (dna_id, version, self._policy.window_id),
        )
        if row is None:
            raise DnaFitnessError(f"DNA fitness snapshot not found: {dna_id}@{version}")
        return _snapshot(row)


def _payload(observation: DnaFitnessObservation, window_id: str) -> dict[str, object]:
    return {
        "dna_id": observation.dna_id, "version": observation.version,
        "content_digest": observation.content_digest,
        "evaluation_id": observation.outcome.evaluation_id, "window_id": window_id,
        "successful": observation.outcome.successful,
        "evidence_score": _score(observation.outcome.evidence.score),
        "user_value_score": _score(observation.outcome.quality.score),
        "cost_minor": observation.cost_minor, "latency_ms": observation.latency_ms,
        "stable": observation.stable, "risk_violations": list(observation.risk_violations),
    }


def _snapshot_values(value: DnaFitnessSnapshot) -> tuple[str | int | float, ...]:
    return (
        value.dna_id, value.version, value.content_digest, value.window_id,
        value.policy_version, value.sample_count, value.success_rate,
        value.success_confidence_lower, value.evidence_score, value.user_value_score,
        value.average_cost_minor, value.average_latency_ms, value.p95_latency_ms,
        value.stability_rate, value.risk_rate, value.readiness.value,
        _time(value.projected_at), value.revision,
    )


def _snapshot(row: sqlite3.Row) -> DnaFitnessSnapshot:
    return DnaFitnessSnapshot(
        str(row["dna_id"]), str(row["version"]), str(row["content_digest"]),
        str(row["window_id"]), str(row["policy_version"]), _int(row["sample_count"]),
        _float(row["success_rate"]), _float(row["success_confidence_lower"]),
        _float(row["evidence_score"]), _float(row["user_value_score"]),
        _float(row["average_cost_minor"]), _float(row["average_latency_ms"]),
        _int(row["p95_latency_ms"]), _float(row["stability_rate"]), _float(row["risk_rate"]),
        FitnessReadiness(str(row["readiness"])), _parse_time(str(row["projected_at"])),
        _int(row["revision"]),
    )


def _readiness(
    count: int, risk_rate: float, now: datetime, policy: DnaFitnessPolicy,
) -> FitnessReadiness:
    if count < policy.minimum_samples:
        return FitnessReadiness.COLLECTING
    if risk_rate > policy.maximum_risk_rate:
        return FitnessReadiness.RISK_BLOCKED
    if now < _utc(policy.ends_at):
        return FitnessReadiness.OBSERVING
    return FitnessReadiness.READY


def _wilson_lower(successes: int, count: int, z: float) -> float:
    rate = successes / count
    denominator = 1 + z * z / count
    centre = rate + z * z / (2 * count)
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * count)) / count)
    return max(0.0, (centre - margin) / denominator)


def _mean(rows: list[sqlite3.Row], field: str) -> float:
    return _round(sum([_float(row[field]) for row in rows]) / len(rows))


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise DnaFitnessError("persisted fitness integer is invalid")
    return value


def _float(value: object) -> float:
    if not isinstance(value, int | float):
        raise DnaFitnessError("persisted fitness number is invalid")
    return float(value)


def _score(value: float | None) -> float:
    return 0.0 if value is None else value


def _round(value: float) -> float:
    return round(value, 6)


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaFitnessError("fitness time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
