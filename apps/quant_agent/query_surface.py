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

    async def _count(self, table: str, where: str | None = None) -> int:
        row = await self._database.fetch_one(
            f"SELECT count(*) AS total FROM {table}" + (f" WHERE {where}" if where else "")
        )
        return 0 if row is None else int(row["total"])

    async def _rows(self, statement: str, params: Sequence[Any]) -> list[dict[str, object]]:
        return [dict(row) for row in await self._database.fetch_all(statement, params)]
