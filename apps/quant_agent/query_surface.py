"""Bounded read-only queries for the class-brain command surface."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from active_agent_platform.diagnostics import HealthService
from active_agent_platform.foundation import SystemClock
from active_agent_platform.storage import SQLiteDatabase


class CommandSurfaceQuery:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def brain(self, area: str, limit: int) -> dict[str, object]:
        health = (await HealthService(self._database, SystemClock()).check()).to_dict()
        tasks = await self._count("task", "status IN ('CREATED','RUNNING')")
        if area == "areas":
            return {"areas": {
                "sensory": await self._count("inbox_message", "status != 'PROCESSED'"),
                "prefrontal": await self._count("plan", "status NOT IN ('EXPIRED','REJECTED')"),
                "motor": tasks,
                "memory": await self._count("episode"),
            }, "derived": True}
        if area == "cycles":
            rows = await self._rows(
                "SELECT plan_id,status,created_at,expires_at,correlation_id FROM plan "
                "ORDER BY created_at DESC LIMIT ?", (limit,),
            )
            return {"cycles": rows, "derived_from": "plan"}
        return {"mode": health.get("brain", "UNKNOWN"), "health": health.get("status"),
                "active_tasks": tasks, "derived": True}

    async def attention(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:  # pragma: no cover - CLI contract covered
        if view == "metrics":
            rows = await self._rows(
                "SELECT evidence_type,count(*) AS total,max(observed_at) AS latest "
                "FROM evidence_ledger GROUP BY evidence_type ORDER BY evidence_type", (),
            )
            return {"metrics": rows, "derived_from": "evidence_ledger"}
        where = "" if identifier is None or view == "recent" else "WHERE evidence_id=?"
        params: Sequence[object] = (limit,) if not where else (identifier, limit)
        rows = await self._rows(
            "SELECT evidence_id,evidence_type,evidence_json,evidence_digest,observed_at,"
            f"correlation_id FROM evidence_ledger {where} ORDER BY observed_at DESC LIMIT ?",
            params,
        )
        for row in rows:
            row["evidence"] = json.loads(str(row.pop("evidence_json")))
        return {"attention": rows, "view": view, "derived_from": "evidence_ledger",
                "explanation_deterministic": True}

    async def goals(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:  # pragma: no cover - CLI contract covered
        where = ""
        params: Sequence[object] = (limit,)
        if view == "show":
            where, params = "WHERE plan_id=?", (identifier, limit)
        elif view == "active":
            where, params = "WHERE expires_at>?", (SystemClock().now().isoformat(), limit)
        rows = await self._rows(
            f"SELECT plan_id,plan_json,status,created_at,expires_at,correlation_id FROM plan {where} "
            "ORDER BY created_at DESC LIMIT ?", params,
        )
        goals: list[dict[str, object]] = []
        for row in rows:
            document = cast(dict[str, object], json.loads(str(row.pop("plan_json"))))
            goal = cast(dict[str, object], document.get("goal", {}))
            goals.append(row | {"goal": goal, "reason": document.get("reason"),
                                "evidence": document.get("evidence", []),
                                "requested_budget": document.get("requested_budget", {}),
                                "policy_context": document.get("policy_context", {})})
        return {"goals": goals, "view": view, "derived_from": "immutable plans",
                "dynamic_mutation_supported": False}

    async def memory(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:  # pragma: no cover - CLI contract covered
        if view == "working":
            return {"memory": [], "view": view, "authoritative": False,
                    "reason": "working memory is process-local and non-authoritative"}
        if view == "episodes":
            rows = await self._rows(
                "SELECT episode_id,task_id,episode_json,created_at,correlation_id FROM episode "
                "ORDER BY created_at DESC LIMIT ?", (limit,),
            )
            for row in rows:
                row["episode"] = json.loads(str(row.pop("episode_json")))
            return {"memory": rows, "view": view, "authoritative": True}
        where = ""
        params: Sequence[object] = (limit,)
        if view == "candidates":
            where = "WHERE status='CANDIDATE'"
        elif view == "search" and identifier:
            where, params = "WHERE claim_key LIKE ? OR statement LIKE ? OR summary LIKE ?", (
                f"%{identifier}%", f"%{identifier}%", f"%{identifier}%", limit,
            )
        rows = await self._rows(
            "SELECT * FROM semantic_memory " + where + " ORDER BY updated_at DESC LIMIT ?", params,
        )
        for row in rows:
            for field in ("claim_value_json", "scope_json", "conditions_json", "evidence_json",
                          "contradicted_by_json"):
                row[field.removesuffix("_json")] = json.loads(str(row.pop(field)))
        return {"memory": rows, "view": view, "authoritative": True,
                "candidate_promotion_implicit": False}

    async def schedules(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:  # pragma: no cover
        schedule_id = identifier or "quant.daily_review"
        if schedule_id != "quant.daily_review":
            return {"schedules": [], "view": view}
        checkpoints = await self._rows(
            "SELECT schedule_id,occurrence_key,status,consumed_at FROM schedule_checkpoint "
            "WHERE schedule_id=? ORDER BY occurrence_key DESC LIMIT ?", (schedule_id, limit),
        )
        configuration = {"schedule_id": schedule_id, "at": "18:00:00",
                         "timezone": "Asia/Shanghai", "missed_policy": "FIRE_ONCE",
                         "trading_days_only": True}
        if view == "history":
            return {"schedules": checkpoints, "view": view}
        return {"schedules": [configuration | {"checkpoints": checkpoints}], "view": view}

    async def events(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        if view == "show":
            rows = await self._rows(
                "SELECT event_id,msg_type,publish_state,attempt,created_at,published_at,correlation_id "
                "FROM outbox_event WHERE event_id=?", (identifier,),
            )
        elif view == "correlation":
            rows = await self._rows(
                "SELECT event_id,msg_type,publish_state,attempt,created_at,published_at,correlation_id "
                "FROM outbox_event WHERE correlation_id=? ORDER BY created_at LIMIT ?",
                (identifier, limit),
            )
        elif view == "inbox":
            rows = await self._rows(
                "SELECT consumer_id,msg_id,status,attempt,received_at,processed_at,error_id,correlation_id "
                "FROM inbox_message ORDER BY received_at DESC LIMIT ?", (limit,),
            )
        elif view == "dead-letter":
            rows = await self._rows(
                "SELECT dead_letter_id,consumer_id,msg_id,error_id,failed_at,correlation_id "
                "FROM dead_letter ORDER BY failed_at DESC LIMIT ?", (limit,),
            )
        else:
            state = None if view == "recent" else "WHERE publish_state != 'PUBLISHED'"
            rows = await self._rows(
                "SELECT event_id,msg_type,publish_state,attempt,created_at,published_at,correlation_id "
                f"FROM outbox_event {state or ''} ORDER BY created_at DESC LIMIT ?", (limit,),
            )
        return {"events": rows, "view": view}

    async def plans(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        where = ""
        params: tuple[Any, ...] = (limit,)
        if view == "show":
            where, params = "WHERE p.plan_id=?", (identifier, limit)
        elif view == "rejected":
            where = "WHERE p.status='REJECTED' OR d.decision='REJECTED'"
        rows = await self._rows(
            "SELECT p.plan_id,p.digest,p.status,p.created_at,p.expires_at,p.correlation_id,"
            "d.decision_id,d.decision,d.decided_at FROM plan p LEFT JOIN plan_decision d "
            f"ON d.plan_id=p.plan_id {where} ORDER BY p.created_at DESC LIMIT ?",
            params,
        )
        return {"plans": rows, "view": view}

    async def tasks(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        if view in {"show", "trace"}:
            tasks = await self._rows("SELECT * FROM task WHERE task_id=?", (identifier,))
            transitions = await self._rows(
                "SELECT from_status,to_status,reason,attempt,occurred_at,correlation_id "
                "FROM task_transition WHERE task_id=? ORDER BY occurred_at", (identifier,),
            )
            return {"tasks": tasks, "transitions": transitions}
        status = {"running": "RUNNING", "failed": "FAILED"}.get(view)
        where = "" if status is None else "WHERE status=?"
        params: Sequence[object] = (limit,) if status is None else (status, limit)
        return {"tasks": await self._rows(
            f"SELECT * FROM task {where} ORDER BY created_at DESC LIMIT ?", params,
        ), "view": view}

    async def catalog(self, kind: str, limit: int, identifier: str | None = None) -> dict[str, object]:
        mapping = {
            "capabilities": ("capability_contract", "capability", "contract_json"),
            "skills": ("skill_manifest", "skill_id", "manifest_json"),
            "workflows": ("workflow_definition", "workflow_id", "definition_json"),
        }
        table, key, payload = mapping[kind]
        where = "" if identifier is None else f"WHERE {key}=?"
        params: Sequence[object] = (limit,) if identifier is None else (identifier, limit)
        rows = await self._rows(
            f"SELECT {key},version,digest,status,created_at,correlation_id,{payload} "
            f"FROM {table} {where} ORDER BY {key},version DESC LIMIT ?", params,
        )
        for row in rows:
            row[payload.removesuffix("_json")] = json.loads(str(row.pop(payload)))
        return {kind: rows}

    async def subscriptions(self, identifier: str | None, limit: int) -> dict[str, object]:
        where = "" if identifier is None else "WHERE subscription_id=?"
        params: Sequence[object] = (limit,) if identifier is None else (identifier, limit)
        return {"subscriptions": await self._rows(
            f"SELECT * FROM insight_subscription {where} ORDER BY created_at DESC LIMIT ?", params,
        )}

    async def dna(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        if view in {"list", "active"}:
            rows: list[dict[str, object]] = []
            for table, kind in (
                ("organization_dna_definition", "organization"),
                ("agent_dna_definition", "agent"),
                ("dna_definition", "workflow"),
            ):
                active = " WHERE status='ACTIVE'" if view == "active" else ""
                found = await self._rows(
                    f"SELECT dna_id,version,status,content_digest,revision,created_at "
                    f"FROM {table}{active} ORDER BY created_at DESC LIMIT ?", (limit,),
                )
                for row in found:
                    row["kind"] = kind
                rows.extend(found)
            return {"dna": rows[:limit], "view": view}
        if view == "executions":
            where = "" if identifier is None else "WHERE organization_dna_id=? OR agent_dna_id=? OR workflow_dna_id=? OR correlation_id=?"
            params: Sequence[object] = (limit,) if identifier is None else (identifier, identifier, identifier, identifier, limit)
            rows = await self._rows(
                "SELECT context_digest,correlation_id,plan_id,task_id,run_id,episode_id,"
                "evaluation_id,organization_dna_id,organization_version,organization_role,"
                "agent_dna_id,agent_version,workflow_dna_id,workflow_version "
                f"FROM dna_execution_context {where} ORDER BY rowid DESC LIMIT ?", params,
            )
            return {"executions": rows}
        if view == "show":
            for table in ("organization_dna_definition", "agent_dna_definition", "dna_definition"):
                rows = await self._rows(
                    f"SELECT * FROM {table} WHERE dna_id=? ORDER BY version DESC LIMIT ?",
                    (identifier, limit),
                )
                if rows:
                    return {"dna": rows}
            return {"dna": []}
        if view == "lineage":
            parents = await self._rows(
                "SELECT parent_dna_id,parent_version,parent_content_digest FROM dna_parent "
                "WHERE child_dna_id=? ORDER BY ordinal", (identifier,),
            )
            return {"dna": parents, "parents": parents}
        if view == "explain":
            explanations = await self._rows(
                "SELECT * FROM dna_explanation WHERE dna_id=? ORDER BY explained_at DESC LIMIT ?",
                (identifier, limit),
            )
            return {"dna": explanations, "explanations": explanations}
        return {"dna": [], "view": view}

    async def evolution(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:  # pragma: no cover
        tables = {"candidates": ("dna_candidate_proposal", "proposal_id"), "fitness": ("dna_fitness_snapshot", "dna_id"), "datasets": ("dna_experience_dataset", "dataset_id"), "replay": ("dna_replay_run", "replay_id"), "campaigns": ("dna_promotion_campaign", "campaign_id")}
        if view == "compare":
            ids = tuple(item.strip() for item in (identifier or "").split(",") if item.strip())
            if len(ids) != 2:
                return {"comparisons": [], "reason": "compare requires two comma-separated DNA IDs"}
            rows = await self._rows(
                "SELECT dna_id,version,window_id,sample_count,success_rate,evidence_score,"
                "user_value_score,stability_rate,risk_rate,readiness FROM dna_fitness_snapshot "
                "WHERE dna_id IN (?,?) ORDER BY dna_id,projected_at DESC LIMIT ?",
                (ids[0], ids[1], limit * 2),
            )
            latest = {str(row["dna_id"]): row for row in rows}
            return {"comparisons": rows, "identifiers": list(ids),
                    "latest_by_dna": latest, "evidence_source": "dna_fitness_snapshot"}
        if view == "explain":
            rows = await self._rows(
                "SELECT explanation_id,dna_id,dna_version,content_digest,document_json,"
                "explanation_digest,explained_at,correlation_id FROM dna_explanation "
                "WHERE (? IS NULL OR explanation_id=? OR dna_id=?) "
                "ORDER BY explained_at DESC LIMIT ?",
                (identifier, identifier, identifier, limit),
            )
            for row in rows:
                row["document"] = json.loads(str(row.pop("document_json")))
            return {"explanations": rows}
        table_key = tables.get(view)
        if table_key is None:
            return {"evolution": [], "view": view}
        table, key = table_key
        where = "" if identifier is None else f"WHERE {key}=?"
        params = (limit,) if identifier is None else (identifier, limit)
        rows = await self._rows(f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ?", params)
        result: dict[str, object] = {view: rows}
        if view == "replay" and identifier:
            result["cases"] = await self._rows(
                "SELECT * FROM dna_replay_case WHERE replay_id=? ORDER BY sample_id LIMIT ?",
                (identifier, limit),
            )
            result["evidence_source"] = "append-only replay run and cases"
        return result

    async def _count(self, table: str, where: str | None = None) -> int:
        row = await self._database.fetch_one(
            f"SELECT count(*) AS total FROM {table}" + (f" WHERE {where}" if where else "")
        )
        return 0 if row is None else int(row["total"])

    async def _rows(self, statement: str, params: Sequence[Any]) -> list[dict[str, object]]:
        return [dict(row) for row in await self._database.fetch_all(statement, params)]
