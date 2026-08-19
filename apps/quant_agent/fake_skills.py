"""Deterministic, dependency-free quant Skills for tests, demos and local E2E runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from active_agent_platform.skills import (
    CapabilityContract,
    CapabilityRegistry,
    ClockPort,
    HealthStatus,
    SideEffect,
    SkillAdapter,
    SkillContext,
    SkillHealth,
    SkillInvocation,
    SkillRegistry,
    SkillResult,
)
from active_agent_platform.storage import SQLiteDatabase

MARKET_CAPABILITY: Final = "market.snapshot.read"
SUMMARY_CAPABILITY: Final = "content.summary.generate"
NOTIFICATION_CAPABILITY: Final = "notification.local.send"


def _object_schema(properties: Mapping[str, object], required: Sequence[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


MARKET_INPUT_SCHEMA = _object_schema(
    {
        "symbols": {"type": "array", "items": {"type": "string"}},
        "trade_date": {"type": "string"},
    },
    ("symbols",),
)
MARKET_OUTPUT_SCHEMA = _object_schema(
    {
        "trade_date": {"type": "string"},
        "quotes": {"type": "array"},
    },
    ("trade_date", "quotes"),
)
SUMMARY_INPUT_SCHEMA = _object_schema(
    {
        "title": {"type": "string"},
        "items": {"type": "array"},
        "max_items": {"type": "integer"},
    },
    ("items",),
)
SUMMARY_OUTPUT_SCHEMA = _object_schema(
    {"summary": {"type": "string"}, "item_count": {"type": "integer"}},
    ("summary", "item_count"),
)
NOTIFICATION_INPUT_SCHEMA = _object_schema(
    {
        "title": {"type": "string"},
        "message": {"type": "string"},
        "level": {"type": "string", "enum": ["INFO", "WARNING", "ERROR"]},
    },
    ("title", "message"),
)
NOTIFICATION_OUTPUT_SCHEMA = _object_schema(
    {"notification_id": {"type": "string"}, "delivered": {"type": "boolean"}},
    ("notification_id", "delivered"),
)


def fake_capability_contracts() -> tuple[CapabilityContract, ...]:
    return (
        CapabilityContract(
            MARKET_CAPABILITY,
            "1.0",
            MARKET_INPUT_SCHEMA,
            MARKET_OUTPUT_SCHEMA,
            SideEffect.PURE,
            frozenset({"market.read"}),
        ),
        CapabilityContract(
            SUMMARY_CAPABILITY,
            "1.0",
            SUMMARY_INPUT_SCHEMA,
            SUMMARY_OUTPUT_SCHEMA,
            SideEffect.PURE,
        ),
        CapabilityContract(
            NOTIFICATION_CAPABILITY,
            "1.0",
            NOTIFICATION_INPUT_SCHEMA,
            NOTIFICATION_OUTPUT_SCHEMA,
            SideEffect.IDEMPOTENT,
            frozenset({"notification.local.write"}),
        ),
    )


def _skill_digest(skill_id: str, version: str) -> str:
    return "sha256:" + hashlib.sha256(f"bia-fake-skill:{skill_id}@{version}".encode()).hexdigest()


def fake_skill_manifests() -> tuple[dict[str, object], ...]:
    specifications: tuple[tuple[str, str, str, list[str], bool], ...] = (
        ("fake-market-read", MARKET_CAPABILITY, "PURE", ["market.read"], False),
        ("fake-summary", SUMMARY_CAPABILITY, "PURE", [], False),
        (
            "local-notification",
            NOTIFICATION_CAPABILITY,
            "IDEMPOTENT",
            ["notification.local.write"],
            True,
        ),
    )
    result: list[dict[str, object]] = []
    for skill_id, capability, side_effect, permissions, queryable in specifications:
        version = "1.0.0"
        result.append(
            {
                "schema_version": "1.0",
                "skill_id": skill_id,
                "version": version,
                "digest": _skill_digest(skill_id, version),
                "provides": [{"capability": capability, "capability_version": "1.0"}],
                "side_effect": side_effect,
                "required_permissions": permissions,
                "runtime": "python",
                "entrypoint": f"apps.quant_agent.fake_skills:{skill_id}",
                "timeout_seconds": 5,
                "concurrency_limit": 8,
                "supports_cancel": True,
                "supports_query": queryable,
                "resources": {
                    "max_cost": 0.0,
                    "max_latency_ms": 10,
                    "memory_mb": 32,
                },
            }
        )
    return tuple(result)


class _FakeAdapter:
    def __init__(self, *, clock: ClockPort) -> None:
        self._clock = clock
        self._cancelled: set[str] = set()
        self._results: dict[str, SkillResult] = {}
        self.invocation_count = 0

    async def health(self) -> SkillHealth:
        return SkillHealth(HealthStatus.HEALTHY, self._now(), 0)

    async def cancel(self, invocation_id: str) -> str:
        self._cancelled.add(invocation_id)
        return "CANCELLED"

    async def query_result(
        self, idempotency_key: str, provider_operation_id: str | None
    ) -> SkillResult:
        del provider_operation_id
        return self._results.get(idempotency_key, SkillResult("UNKNOWN"))

    def _before_invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult | None:
        if invocation.invocation_id in self._cancelled or context.cancellation.cancelled:
            return SkillResult("CANCELLED")
        self.invocation_count += 1
        return None

    def _now(self) -> datetime:
        return self._clock.now().astimezone(UTC)


class FakeMarketRead(_FakeAdapter):
    """Return stable synthetic quotes derived only from symbol and trade date."""

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        early = self._before_invoke(invocation, context)
        if early is not None:
            return early
        symbols = invocation.input.get("symbols")
        trade_date = invocation.input.get("trade_date", self._now().date().isoformat())
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
            or not isinstance(trade_date, str)
        ):
            return SkillResult("FAILED", {"code": "SKILL_INPUT_INVALID"})
        quotes: list[dict[str, object]] = []
        for symbol in symbols:
            seed = int(hashlib.sha256(f"{trade_date}:{symbol}".encode()).hexdigest()[:8], 16)
            price = round(5 + seed % 50_000 / 1000, 3)
            quotes.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "volume": 1_000 + seed % 999_000,
                    "as_of": f"{trade_date}T07:00:00Z",
                }
            )
        return SkillResult("SUCCEEDED", {"trade_date": trade_date, "quotes": quotes})


class FakeSummary(_FakeAdapter):
    """Generate a deterministic local summary without an LLM dependency."""

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        early = self._before_invoke(invocation, context)
        if early is not None:
            return early
        items = invocation.input.get("items")
        title = invocation.input.get("title", "Summary")
        max_items = invocation.input.get("max_items", 5)
        if (
            not isinstance(items, list)
            or not isinstance(title, str)
            or not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or max_items < 1
        ):
            return SkillResult("FAILED", {"code": "SKILL_INPUT_INVALID"})
        selected = [str(item) for item in items[:max_items]]
        body = "; ".join(selected) if selected else "No items."
        return SkillResult(
            "SUCCEEDED",
            {"summary": f"{title}: {body}", "item_count": len(items)},
        )


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    notification_id: str
    title: str
    message: str
    level: str
    delivered_at: datetime


class LocalNotification(_FakeAdapter):
    """Local idempotent sink with optional restart-safe SQLite persistence."""

    def __init__(self, *, clock: ClockPort, database: SQLiteDatabase | None = None) -> None:
        super().__init__(clock=clock)
        self._database = database
        self._records: list[NotificationRecord] = []
        self._payload_digests: dict[str, str] = {}

    @property
    def records(self) -> tuple[NotificationRecord, ...]:
        return tuple(self._records)

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        early = self._before_invoke(invocation, context)
        if early is not None:
            return early
        previous = await self._previous(invocation.idempotency_key)
        if previous is not None:
            result, digest = previous
            return result if digest == _notification_digest(invocation.input) else SkillResult(
                "FAILED", {"code": "IDEMPOTENCY_CONFLICT"}
            )
        title = invocation.input.get("title")
        message = invocation.input.get("message")
        level = invocation.input.get("level", "INFO")
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(message, str)
            or not message
            or level not in {"INFO", "WARNING", "ERROR"}
        ):
            return SkillResult("FAILED", {"code": "SKILL_INPUT_INVALID"})
        notification_id = hashlib.sha256(invocation.idempotency_key.encode()).hexdigest()[:24]
        delivered_at = self._now()
        record = NotificationRecord(notification_id, title, message, str(level), delivered_at)
        result = SkillResult(
            "SUCCEEDED",
            {"notification_id": notification_id, "delivered": True},
            notification_id,
        )
        if self._database is not None:
            try:
                async with self._database.transaction() as transaction:
                    await transaction.execute(
                        "INSERT INTO local_notification_delivery VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            notification_id, invocation.idempotency_key,
                            _notification_digest(invocation.input), title, message, str(level),
                            _result_json(result), delivered_at.isoformat().replace("+00:00", "Z"),
                            invocation.task_id, invocation.run_id,
                        ),
                    )
            except sqlite3.IntegrityError:
                concurrent = await self._previous(invocation.idempotency_key)
                if concurrent is None:
                    raise
                stored, digest = concurrent
                return stored if digest == _notification_digest(invocation.input) else SkillResult(
                    "FAILED", {"code": "IDEMPOTENCY_CONFLICT"}
                )
        self._records.append(record)
        self._results[invocation.idempotency_key] = result
        self._payload_digests[invocation.idempotency_key] = _notification_digest(invocation.input)
        context.logger.info(
            "local notification delivered",
            notification_id=notification_id,
            level=level,
        )
        return result

    async def query_result(
        self, idempotency_key: str, provider_operation_id: str | None
    ) -> SkillResult:
        del provider_operation_id
        previous = await self._previous(idempotency_key)
        return SkillResult("UNKNOWN") if previous is None else previous[0]

    async def persisted_records(self) -> tuple[NotificationRecord, ...]:
        if self._database is None:
            return self.records
        rows = await self._database.fetch_all(
            "SELECT * FROM local_notification_delivery ORDER BY delivered_at, notification_id"
        )
        return tuple(
            NotificationRecord(
                str(row["notification_id"]), str(row["title"]), str(row["message"]),
                str(row["level"]), datetime.fromisoformat(str(row["delivered_at"])),
            )
            for row in rows
        )

    async def _previous(self, idempotency_key: str) -> tuple[SkillResult, str] | None:
        cached = self._results.get(idempotency_key)
        if cached is not None:
            return cached, self._payload_digests[idempotency_key]
        if self._database is None:
            return None
        row = await self._database.fetch_one(
            "SELECT result_json, payload_digest FROM local_notification_delivery WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if row is None:
            return None
        value = json.loads(str(row["result_json"]))
        result = SkillResult(str(value["status"]), value.get("output"), value.get("provider_operation_id"))
        self._results[idempotency_key] = result
        digest = str(row["payload_digest"])
        self._payload_digests[idempotency_key] = digest
        return result, digest


@dataclass(frozen=True, slots=True)
class FakeSkillBundle:
    market: FakeMarketRead
    summary: FakeSummary
    notification: LocalNotification

    @property
    def adapters(self) -> Mapping[tuple[str, str], SkillAdapter]:
        return {
            ("fake-market-read", "1.0.0"): self.market,
            ("fake-summary", "1.0.0"): self.summary,
            ("local-notification", "1.0.0"): self.notification,
        }


def install_fake_skills(
    capabilities: CapabilityRegistry,
    skills: SkillRegistry,
    *,
    clock: ClockPort,
    database: SQLiteDatabase | None = None,
) -> FakeSkillBundle:
    """Register contracts, install/verify/health-enable Skills and return adapters."""
    for contract in fake_capability_contracts():
        capabilities.register(contract)
    bundle = FakeSkillBundle(
        FakeMarketRead(clock=clock),
        FakeSummary(clock=clock),
        LocalNotification(clock=clock, database=database),
    )
    for manifest in fake_skill_manifests():
        skill_id, version, digest = (
            str(manifest["skill_id"]),
            str(manifest["version"]),
            str(manifest["digest"]),
        )
        skills.install(manifest, package_digest=digest)
        skills.verify(skill_id, version)
        skills.enable(skill_id, version, _bootstrap_health(clock))
    return bundle


def _bootstrap_health(clock: ClockPort) -> SkillHealth:
    """Bootstrap health evidence synchronously for dependency-free composition."""
    return SkillHealth(HealthStatus.HEALTHY, clock.now(), 0)


def _notification_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _result_json(result: SkillResult) -> str:
    return json.dumps(
        {
            "status": result.status,
            "output": result.output,
            "provider_operation_id": result.provider_operation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
