"""G04 deterministic rest-period repair and daily Episode review requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock, UuidGenerator


class RepairError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RepairStatus(StrEnum):
    REQUESTED = "REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RepairOutcome(StrEnum):
    REQUESTED = "REQUESTED"
    DUPLICATE = "DUPLICATE"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True, slots=True)
class DailyReviewSummary:
    business_date: date
    total: int
    successful: int
    failed: int
    unknown: int
    episode_ids: tuple[str, ...]
    classification: str

    def to_dict(self) -> dict[str, object]:
        return {
            "business_date": self.business_date.isoformat(),
            "total": self.total,
            "successful": self.successful,
            "failed": self.failed,
            "unknown": self.unknown,
            "episode_ids": list(self.episode_ids),
            "classification": self.classification,
        }


@dataclass(frozen=True, slots=True)
class RepairRequest:
    run_id: str
    review_key: str
    workflow_id: str
    workflow_version: str
    parameters: Mapping[str, object]
    deadline: datetime
    correlation_id: str
    attempt: int
    requires_model: bool


@dataclass(frozen=True, slots=True)
class RepairDecision:
    outcome: RepairOutcome
    request: RepairRequest | None
    summary: DailyReviewSummary | None
    reason: str


class RestRepair:
    def __init__(
        self,
        database: SQLiteDatabase,
        clock: Clock,
        identifiers: UuidGenerator,
        *,
        timezone: str = "Asia/Shanghai",
        workflow_id: str = "daily_review",
        workflow_version: str = "1.0.0",
        deadline_seconds: int = 60,
        max_attempts: int = 3,
    ) -> None:
        try:
            self._zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA zone") from error
        if not workflow_id or not workflow_version:
            raise ValueError("review workflow identity must not be empty")
        if deadline_seconds < 1 or max_attempts < 1:
            raise ValueError("deadline and attempts must be positive")
        self._database = database
        self._clock = clock
        self._identifiers = identifiers
        self._workflow_id = workflow_id
        self._workflow_version = workflow_version
        self._deadline_seconds = deadline_seconds
        self._max_attempts = max_attempts

    async def prepare(
        self, business_date: date, *, mode: str, phase: str,
    ) -> RepairDecision:
        if mode != "REVIEW" or phase not in {"CLOSED", "HOLIDAY"}:
            return RepairDecision(RepairOutcome.INELIGIBLE, None, None, "rest review state is not active")
        now = _utc(self._clock.now())
        review_key = f"daily_review:{business_date.isoformat()}"
        existing = await self._database.fetch_one(
            "SELECT * FROM rest_repair_run WHERE review_key = ?", (review_key,)
        )
        if existing is not None:
            status = RepairStatus(str(existing["status"]))
            attempt = int(existing["attempt"])
            deadline = _datetime(existing["deadline"])
            if status is not RepairStatus.FAILED:
                return RepairDecision(RepairOutcome.DUPLICATE, None, None, "daily review already requested")
            if attempt >= self._max_attempts or now >= deadline:
                return RepairDecision(RepairOutcome.DUPLICATE, None, None, "daily review retry limit reached")
            summary = await self._summarize(business_date)
            request = self._request(
                str(existing["run_id"]), review_key, summary, deadline,
                str(existing["correlation_id"]), attempt + 1,
            )
            async with self._database.transaction() as transaction:
                await transaction.execute(
                    "UPDATE rest_repair_run SET status = 'REQUESTED', attempt = ?, request_json = ?, updated_at = ? WHERE run_id = ? AND status = 'FAILED'",
                    (request.attempt, _json(request.parameters), _time(now), request.run_id),
                )
            return RepairDecision(RepairOutcome.REQUESTED, request, summary, "failed review scheduled for retry")

        summary = await self._summarize(business_date)
        run_id, correlation_id = str(self._identifiers.new()), str(self._identifiers.new())
        deadline = now + timedelta(seconds=self._deadline_seconds)
        request = self._request(run_id, review_key, summary, deadline, correlation_id, 1)
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "INSERT INTO rest_repair_run VALUES (?, ?, ?, 'REQUESTED', 1, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    run_id, review_key, business_date.isoformat(), self._workflow_id,
                    self._workflow_version, _json(request.parameters), _time(now), _time(now),
                    _time(deadline), correlation_id,
                ),
            )
        return RepairDecision(RepairOutcome.REQUESTED, request, summary, "daily review requested")

    async def complete(
        self, run_id: str, *, result: Mapping[str, object], candidate_experiences: tuple[Mapping[str, object], ...] = (),
    ) -> None:
        row = await self._database.fetch_one("SELECT * FROM rest_repair_run WHERE run_id = ?", (run_id,))
        if row is None:
            raise RepairError("REPAIR_NOT_FOUND", "repair run does not exist")
        if RepairStatus(str(row["status"])) is not RepairStatus.REQUESTED:
            raise RepairError("REPAIR_NOT_ACTIVE", "repair run is not awaiting completion")
        request = json.loads(str(row["request_json"]))
        allowed_evidence = set(request["summary"]["episode_ids"])
        for candidate in candidate_experiences:
            if candidate.get("status") != "CANDIDATE":
                raise RepairError("EXPERIENCE_STATE_INVALID", "repair may only produce candidate experience")
            evidence = candidate.get("evidence_episode_ids")
            if not isinstance(evidence, list) or not evidence or not set(evidence) <= allowed_evidence:
                raise RepairError("EXPERIENCE_EVIDENCE_INVALID", "candidate evidence must reference reviewed Episodes")
        document = {"result": dict(result), "candidate_experiences": [dict(item) for item in candidate_experiences]}
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "UPDATE rest_repair_run SET status = 'SUCCEEDED', result_json = ?, updated_at = ? WHERE run_id = ? AND status = 'REQUESTED'",
                (_json(document), _time(self._clock.now()), run_id),
            )

    async def fail(self, run_id: str, *, error_code: str) -> None:
        if not error_code:
            raise ValueError("error_code must not be empty")
        async with self._database.transaction() as transaction:
            cursor = await transaction.execute(
                "UPDATE rest_repair_run SET status = 'FAILED', result_json = ?, updated_at = ? WHERE run_id = ? AND status = 'REQUESTED'",
                (_json({"error_code": error_code}), _time(self._clock.now()), run_id),
            )
            if cursor.rowcount != 1:
                raise RepairError("REPAIR_NOT_ACTIVE", "repair run is not awaiting failure")

    async def _summarize(self, business_date: date) -> DailyReviewSummary:
        start = datetime.combine(business_date, time.min, tzinfo=self._zone).astimezone(UTC)
        end = (datetime.combine(business_date, time.min, tzinfo=self._zone) + timedelta(days=1)).astimezone(UTC)
        rows = await self._database.fetch_all(
            "SELECT episode_id, episode_json FROM episode WHERE created_at >= ? AND created_at < ? ORDER BY created_at, episode_id",
            (_time(start), _time(end)),
        )
        successful = failed = unknown = 0
        ids: list[str] = []
        for row in rows:
            ids.append(str(row["episode_id"]))
            document = json.loads(str(row["episode_json"]))
            value = _successful(document)
            if value is True:
                successful += 1
            elif value is False:
                failed += 1
            else:
                unknown += 1
        classification = "NO_ACTIVITY" if not rows else "ACTIVITY"
        return DailyReviewSummary(business_date, len(rows), successful, failed, unknown, tuple(ids), classification)

    def _request(
        self, run_id: str, review_key: str, summary: DailyReviewSummary,
        deadline: datetime, correlation_id: str, attempt: int,
    ) -> RepairRequest:
        parameters = {"review_key": review_key, "summary": summary.to_dict()}
        return RepairRequest(
            run_id, review_key, self._workflow_id, self._workflow_version, parameters,
            deadline, correlation_id, attempt, summary.total > 0,
        )


def _successful(document: object) -> bool | None:
    if not isinstance(document, dict):
        return None
    evaluation = document.get("evaluation")
    if isinstance(evaluation, dict) and isinstance(evaluation.get("successful"), bool):
        return bool(evaluation["successful"])
    if isinstance(document.get("successful"), bool):
        return bool(document["successful"])
    return None


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RepairError("TIME_INVALID", "repair time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value))
