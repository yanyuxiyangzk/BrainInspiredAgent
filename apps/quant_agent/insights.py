"""I04 read-only MarketInsight projection over authoritative execution facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from active_agent_platform.storage import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class MarketInsight:
    insight_id: str
    title: str
    summary: str
    delivered_at: datetime
    fresh_until: datetime
    stale: bool
    evidence: tuple[Mapping[str, object], ...]
    risk_reasons: tuple[str, ...]
    workflow_version: str
    evaluator_version: str
    correlation_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "insight_id": self.insight_id, "title": self.title, "summary": self.summary,
            "delivered_at": _time(self.delivered_at), "fresh_until": _time(self.fresh_until),
            "stale": self.stale, "evidence": [dict(item) for item in self.evidence],
            "risk_reasons": list(self.risk_reasons), "workflow_version": self.workflow_version,
            "evaluator_version": self.evaluator_version, "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class InsightExplanation:
    insight: MarketInsight
    plan_id: str
    decision_id: str
    grant_id: str
    task_id: str
    run_id: str


class MarketInsightQuery:
    """Serve latest/show/explain without mutating source facts."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def latest(
        self, *, limit: int = 10, now: datetime | None = None,
        cursor: str | None = None, stale: str = "include", symbol: str | None = None,
        since: datetime | None = None, until: datetime | None = None,
        insight_type: str | None = None,
    ) -> tuple[MarketInsight, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if stale not in {"include", "exclude", "only"}:
            raise ValueError("stale must be include, exclude or only")
        if since is not None and until is not None and since > until:
            raise ValueError("since must not be later than until")
        rows = await self._database.fetch_all(
            """SELECT notification_id FROM local_notification_delivery
               WHERE (? IS NULL OR notification_id < ?)
               ORDER BY delivered_at DESC, notification_id DESC LIMIT ?""", (cursor, cursor, 101)
        )
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        values = tuple([await self._load(str(row["notification_id"]), instant) for row in rows])
        filtered = tuple(item for item in values if (
            (stale == "include" or item.stale == (stale == "only"))
            and (symbol is None or any(str(evidence.get("symbol", "")) == symbol for evidence in item.evidence))
            and (since is None or item.delivered_at >= since.astimezone(UTC))
            and (until is None or item.delivered_at <= until.astimezone(UTC))
            and (insight_type is None or insight_type == "market_summary")
        ))
        return filtered[:limit]

    async def show(self, insight_id: str, *, now: datetime | None = None) -> MarketInsight:
        if not insight_id:
            raise ValueError("insight_id must not be empty")
        return await self._load(insight_id, (now or datetime.now(UTC)).astimezone(UTC))

    async def explain(self, insight_id: str, *, now: datetime | None = None) -> InsightExplanation:
        insight = await self.show(insight_id, now=now)
        row = await self._database.fetch_one(
            """SELECT p.plan_id, d.decision_id, g.grant_id, t.task_id, w.run_id
               FROM local_notification_delivery n
               JOIN workflow_run w ON w.run_id = n.run_id
               JOIN task t ON t.task_id = w.task_id
               JOIN execution_grant g ON g.grant_id = t.grant_id
               JOIN plan_decision d ON d.decision_id = g.decision_id
               JOIN plan p ON p.plan_id = d.plan_id
               WHERE n.notification_id = ?""", (insight_id,)
        )
        if row is None:
            raise LookupError("market insight does not exist")
        return InsightExplanation(insight, *(str(row[key]) for key in
            ("plan_id", "decision_id", "grant_id", "task_id", "run_id")))

    async def _load(self, insight_id: str, now: datetime) -> MarketInsight:
        row = await self._database.fetch_one(
            """SELECT n.*, w.workflow_version, w.correlation_id, p.plan_json,
                      d.decision_json, o.evaluation_json,
                      r.output_json AS read_output
               FROM local_notification_delivery n
               JOIN workflow_run w ON w.run_id = n.run_id
               JOIN task t ON t.task_id = w.task_id
               JOIN execution_grant g ON g.grant_id = t.grant_id
               JOIN plan_decision d ON d.decision_id = g.decision_id
               JOIN plan p ON p.plan_id = d.plan_id
               LEFT JOIN outcome_evaluation o ON o.task_id = t.task_id
               LEFT JOIN node_run r ON r.run_id = w.run_id AND r.node_id = 'read_snapshot'
               WHERE n.notification_id = ?""", (insight_id,)
        )
        if row is None:
            raise LookupError("market insight does not exist")
        plan, decision = _object(row["plan_json"]), _object(row["decision_json"])
        outcome = _object(row["evaluation_json"]) if row["evaluation_json"] else {}
        read = _object(row["read_output"]) if row["read_output"] else {}
        evidence_raw = read.get("quotes", [])
        evidence = tuple(item for item in evidence_raw if isinstance(item, Mapping)) \
            if isinstance(evidence_raw, list) else ()
        context = plan.get("policy_context", {})
        fresh = datetime.fromisoformat(str(context["data_fresh_until"])) \
            if isinstance(context, Mapping) else datetime.fromisoformat(str(row["delivered_at"]))
        reasons = decision.get("reasons", [])
        return MarketInsight(
            str(row["notification_id"]), str(row["title"]), str(row["message"]),
            datetime.fromisoformat(str(row["delivered_at"])).astimezone(UTC), fresh.astimezone(UTC),
            now > fresh, evidence,
            tuple(str(item) for item in reasons) if isinstance(reasons, list) else (),
            str(row["workflow_version"]), str(outcome.get("evaluator_version", "unknown")),
            str(row["correlation_id"]),
        )


def _object(value: object) -> dict[str, object]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise TypeError("stored insight source must be a JSON object")
    return decoded


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
