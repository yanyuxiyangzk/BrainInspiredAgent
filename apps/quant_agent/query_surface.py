"""Bounded read-only queries for the class-brain command surface."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

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

    async def attention(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        rows = await self._safe_rows("evidence_ledger", limit, identifier, "evidence_id")
        return {"attention": rows, "view": view, "derived": True}

    async def goals(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        rows = await self._safe_rows("plan", limit, identifier, "plan_id")
        return {"goals": rows, "view": view, "derived": True}

    async def memory(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        table = "semantic_memory" if view in {"semantic", "search", "candidates"} else "episode"
        rows = await self._safe_rows(table, limit, identifier, None)
        return {"memory": rows, "view": view, "derived": view in {"working", "candidates"}}

    async def schedules(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        rows = await self._safe_rows("schedule_checkpoint", limit, identifier, "schedule_id")
        return {"schedules": rows, "view": view}

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
                rows.extend(await self._rows(
                    f"SELECT dna_id,version,status,content_digest,revision,created_at "
                    f"FROM {table}{active} ORDER BY created_at DESC LIMIT ?", (limit,),
                ))
                for row in rows[-limit:]:
                    row["kind"] = kind
            return {"dna": rows[:limit], "view": view}
        if view == "executions":
            rows = await self._rows(
                "SELECT context_digest,correlation_id,plan_id,task_id,run_id,episode_id,"
                "evaluation_id,organization_dna_id,organization_version,organization_role,"
                "agent_dna_id,agent_version,workflow_dna_id,workflow_version "
                "FROM dna_execution_context ORDER BY rowid DESC LIMIT ?", (limit,),
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

    async def evolution(self, view: str, limit: int, identifier: str | None) -> dict[str, object]:
        tables = {"candidates": ("dna_candidate_proposal", "proposal_id"), "fitness": ("dna_fitness_snapshot", "dna_id"), "datasets": ("dna_experience_dataset", "dataset_id"), "replay": ("dna_replay_run", "replay_id"), "campaigns": ("dna_promotion_campaign", "campaign_id")}
        if view == "compare":
            return {"comparisons": []}
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
        return {view: await self._rows(f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ?", params)}

    async def _count(self, table: str, where: str | None = None) -> int:
        row = await self._database.fetch_one(
            f"SELECT count(*) AS total FROM {table}" + (f" WHERE {where}" if where else "")
        )
        return 0 if row is None else int(row["total"])

    async def _rows(self, statement: str, params: Sequence[Any]) -> list[dict[str, object]]:
        return [dict(row) for row in await self._database.fetch_all(statement, params)]

    async def _safe_rows(self, table: str, limit: int, identifier: str | None,
                         key: str | None) -> list[dict[str, object]]:
        exists = await self._database.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if exists is None:
            return []
        where = "" if identifier is None or key is None else f"WHERE {key}=?"
        params: Sequence[object] = (limit,) if not where else (identifier, limit)
        return await self._rows(f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ?", params)
