"""G02 deterministic execution, goal, quality and evidence evaluation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from active_agent_platform.events import EventEnvelope
from active_agent_platform.events.outbox import OutboxWriter
from active_agent_platform.motor import TaskStatus
from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator


class OutcomeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AssessmentStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OutcomePolicy:
    evaluator_version: str
    minimum_quality_score: float = 0.7
    baseline_tolerance: float = 0.0
    maximum_cost_ratio: float = 1.0

    def __post_init__(self) -> None:
        if not self.evaluator_version:
            raise ValueError("evaluator_version must not be empty")
        if not 0 <= self.minimum_quality_score <= 1:
            raise ValueError("minimum_quality_score must be between zero and one")
        if not 0 <= self.baseline_tolerance <= 1:
            raise ValueError("baseline_tolerance must be between zero and one")
        if self.maximum_cost_ratio < 0:
            raise ValueError("maximum_cost_ratio must be non-negative")


@dataclass(frozen=True, slots=True)
class OutcomeRequest:
    task_id: str
    correlation_id: str
    task_status: TaskStatus
    goal_id: str
    goal_completed: bool | None
    quality_metrics: Mapping[str, float]
    baseline_quality: float | None
    cost_ratio: float
    risk_violations: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    required_evidence: int

    def __post_init__(self) -> None:
        if not self.task_id or not self.correlation_id or not self.goal_id:
            raise ValueError("outcome identifiers must not be empty")
        if self.task_status not in _TERMINAL_TASKS:
            raise ValueError("only a terminal task can be evaluated")
        if self.required_evidence < 0 or self.cost_ratio < 0:
            raise ValueError("evidence count and cost ratio must be non-negative")
        if self.baseline_quality is not None and not 0 <= self.baseline_quality <= 1:
            raise ValueError("baseline quality must be between zero and one")
        if any(not 0 <= score <= 1 for score in self.quality_metrics.values()):
            raise ValueError("quality metrics must be between zero and one")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence IDs must be unique")
        object.__setattr__(self, "quality_metrics", MappingProxyType(dict(self.quality_metrics)))


@dataclass(frozen=True, slots=True)
class Assessment:
    status: AssessmentStatus
    score: float | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "score": self.score, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class OutcomeEvaluation:
    evaluation_id: str
    episode_id: str
    task_id: str
    correlation_id: str
    evaluated_at: datetime
    evaluator_version: str
    task_status: TaskStatus
    goal_id: str
    evidence_ids: tuple[str, ...]
    execution: Assessment
    goal: Assessment
    quality: Assessment
    evidence: Assessment

    @property
    def successful(self) -> bool:
        return self.execution.status is AssessmentStatus.PASSED and self.goal.status is AssessmentStatus.PASSED

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluation_id": self.evaluation_id,
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "evaluated_at": _time(self.evaluated_at),
            "evaluator_version": self.evaluator_version,
            "task_status": self.task_status,
            "goal_id": self.goal_id,
            "evidence_ids": list(self.evidence_ids),
            "execution": self.execution.to_dict(),
            "goal": self.goal.to_dict(),
            "quality": self.quality.to_dict(),
            "evidence": self.evidence.to_dict(),
            "successful": self.successful,
        }


class OutcomeRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def add(self, evaluation: OutcomeEvaluation) -> None:
        task = await self._transaction.fetch_one(
            "SELECT status, correlation_id FROM task WHERE task_id = ?", (evaluation.task_id,)
        )
        if task is None:
            raise OutcomeError("TASK_NOT_FOUND", "evaluated task does not exist")
        if str(task["correlation_id"]) != evaluation.correlation_id:
            raise OutcomeError("CORRELATION_MISMATCH", "task correlation does not match evaluation")
        if str(task["status"]) != evaluation.task_status:
            raise OutcomeError("TASK_STATUS_MISMATCH", "persisted task status does not match evaluation")
        try:
            await self._transaction.execute(
                "INSERT INTO episode VALUES (?, ?, ?, ?, ?)",
                (
                    evaluation.episode_id,
                    evaluation.task_id,
                    _json({"kind": "TASK_OUTCOME", "evaluation": evaluation.to_dict()}),
                    _time(evaluation.evaluated_at),
                    evaluation.correlation_id,
                ),
            )
            await self._transaction.execute(
                "INSERT INTO outcome_evaluation VALUES (?, ?, ?, ?, ?, ?)",
                (
                    evaluation.evaluation_id,
                    evaluation.task_id,
                    evaluation.episode_id,
                    _json(evaluation.to_dict()),
                    _time(evaluation.evaluated_at),
                    evaluation.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise OutcomeError("OUTCOME_ALREADY_EXISTS", "outcome or episode already exists") from error


class OutcomeEvaluator:
    def __init__(
        self,
        database: SQLiteDatabase,
        clock: Clock,
        identifiers: UuidGenerator,
        policy: OutcomePolicy,
        *,
        outbox: OutboxWriter | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._identifiers = identifiers
        self._policy = policy
        self._outbox = outbox or OutboxWriter(clock)

    @property
    def version(self) -> str:
        return self._policy.evaluator_version

    def evaluate(self, request: OutcomeRequest) -> OutcomeEvaluation:
        now = _utc(self._clock.now())
        return OutcomeEvaluation(
            str(self._identifiers.new()),
            str(self._identifiers.new()),
            request.task_id,
            request.correlation_id,
            now,
            self._policy.evaluator_version,
            request.task_status,
            request.goal_id,
            request.evidence_ids,
            _execution(request.task_status),
            _goal(request.goal_completed),
            _quality(request, self._policy),
            _evidence(request.evidence_ids, request.required_evidence),
        )

    async def evaluate_and_record(self, request: OutcomeRequest) -> OutcomeEvaluation:
        evaluation = self.evaluate(request)
        event = EventEnvelope(
            msg_id=str(self._identifiers.new()),
            msg_type="outcome.evaluated",
            source="outcome.evaluator",
            occurred_at=evaluation.evaluated_at,
            published_at=evaluation.evaluated_at,
            priority=70,
            correlation_id=evaluation.correlation_id,
            causation_id=evaluation.task_id,
            dedup_key=f"outcome:{evaluation.evaluation_id}",
            payload={
                "event_type": "outcome.evaluated",
                "stimulus_id": evaluation.task_id,
                "data": evaluation.to_dict(),
                "data_quality": "VALID",
            },
        )
        async with self._database.transaction() as transaction:
            await OutcomeRepository(transaction).add(evaluation)
            await self._outbox.append(transaction, event)
        return evaluation


_TERMINAL_TASKS = frozenset({
    TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.CANCELLED,
    TaskStatus.EXPIRED, TaskStatus.REQUIRES_REVIEW,
})


def _execution(status: TaskStatus) -> Assessment:
    if status is TaskStatus.SUCCEEDED:
        return Assessment(AssessmentStatus.PASSED, 1.0, ("task_succeeded",))
    if status is TaskStatus.REQUIRES_REVIEW:
        return Assessment(AssessmentStatus.UNKNOWN, None, ("task_requires_review",))
    return Assessment(AssessmentStatus.FAILED, 0.0, (f"task_{status.value.lower()}",))


def _goal(completed: bool | None) -> Assessment:
    if completed is True:
        return Assessment(AssessmentStatus.PASSED, 1.0, ("goal_completed",))
    if completed is False:
        return Assessment(AssessmentStatus.FAILED, 0.0, ("goal_not_completed",))
    return Assessment(AssessmentStatus.UNKNOWN, None, ("goal_result_unavailable",))


def _quality(request: OutcomeRequest, policy: OutcomePolicy) -> Assessment:
    if not request.quality_metrics:
        return Assessment(AssessmentStatus.UNKNOWN, None, ("quality_metrics_missing",))
    score = sum(request.quality_metrics.values()) / len(request.quality_metrics)
    reasons: list[str] = []
    if score < policy.minimum_quality_score:
        reasons.append("quality_below_threshold")
    if request.baseline_quality is not None and score + policy.baseline_tolerance < request.baseline_quality:
        reasons.append("quality_below_baseline")
    if request.cost_ratio > policy.maximum_cost_ratio:
        reasons.append("cost_budget_exceeded")
    if request.risk_violations:
        reasons.append("risk_policy_violated")
    status = AssessmentStatus.PASSED if not reasons else AssessmentStatus.FAILED
    return Assessment(status, round(score, 6), tuple(reasons or ["quality_acceptable"]))


def _evidence(ids: tuple[str, ...], required: int) -> Assessment:
    if required == 0 or len(ids) >= required:
        return Assessment(AssessmentStatus.PASSED, 1.0, ("evidence_sufficient",))
    if not ids:
        return Assessment(AssessmentStatus.FAILED, 0.0, ("evidence_missing",))
    return Assessment(
        AssessmentStatus.PARTIAL,
        round(len(ids) / required, 6),
        ("evidence_incomplete",),
    )


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OutcomeError("TIME_INVALID", "evaluation time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")
