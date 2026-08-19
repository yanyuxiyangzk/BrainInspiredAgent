"""I02 low-cardinality operational metrics and durable fact projections."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    captured_at: datetime
    loop_lag_seconds: float
    queues: Mapping[str, int]
    tasks: Mapping[str, int]
    skills: Mapping[str, int]
    model_requests: int
    model_tokens: int
    model_cost_minor: int
    model_cache_hits: int
    side_effect_deliveries: int
    duplicate_side_effects: int

    def to_dict(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "loop_lag_seconds": self.loop_lag_seconds,
            "queues": dict(self.queues), "tasks": dict(self.tasks), "skills": dict(self.skills),
            "model": {"requests": self.model_requests, "tokens": self.model_tokens,
                      "cost_minor": self.model_cost_minor, "cache_hits": self.model_cache_hits},
            "side_effects": {"deliveries": self.side_effect_deliveries,
                             "duplicates": self.duplicate_side_effects},
        }


class PlatformMetrics:
    """Combine durable SQLite facts with process-local loop/model observations."""

    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database, self._clock = database, clock
        self._loop_lag = 0.0
        self._model_requests = self._model_tokens = self._model_cost = self._cache_hits = 0
        self._duplicate_side_effects = 0

    def observe_loop(self, expected_at: datetime) -> None:
        if expected_at.tzinfo is None or expected_at.utcoffset() is None:
            raise ValueError("expected loop time must be timezone-aware")
        self._loop_lag = max(
            0.0, (self._clock.now().astimezone(UTC) - expected_at.astimezone(UTC)).total_seconds()
        )

    def record_model(self, *, tokens: int, cost_minor: int, cache_hit: bool = False) -> None:
        if tokens < 0 or cost_minor < 0:
            raise ValueError("model usage cannot be negative")
        self._model_requests += 1
        self._model_tokens += tokens
        self._model_cost += cost_minor
        self._cache_hits += int(cache_hit)

    def record_duplicate_side_effect(self) -> None:
        self._duplicate_side_effects += 1

    async def snapshot(self) -> MetricsSnapshot:
        queues = {
            "inbox_pending": await self._count("inbox_message", "status != 'DONE'"),
            "outbox_pending": await self._count("outbox_event", "publish_state = 'PENDING'"),
            "dead_letter": await self._count("dead_letter"),
        }
        tasks = await self._group("task", "status")
        skills = await self._group("node_run", "status")
        deliveries = await self._count("local_notification_delivery")
        return MetricsSnapshot(
            self._clock.now().astimezone(UTC), self._loop_lag,
            MappingProxyType(queues), MappingProxyType(tasks), MappingProxyType(skills),
            self._model_requests, self._model_tokens, self._model_cost, self._cache_hits,
            deliveries, self._duplicate_side_effects,
        )

    async def _count(self, table: str, condition: str | None = None) -> int:
        suffix = "" if condition is None else f" WHERE {condition}"
        row = await self._database.fetch_one(f"SELECT count(*) AS value FROM {table}{suffix}")
        return 0 if row is None else int(row["value"])

    async def _group(self, table: str, column: str) -> dict[str, int]:
        rows = await self._database.fetch_all(
            f"SELECT {column} AS label, count(*) AS value FROM {table} GROUP BY {column}"
        )
        return dict(Counter({str(row["label"]): int(row["value"]) for row in rows}))


def prometheus(snapshot: MetricsSnapshot) -> str:
    """Render stable Prometheus exposition without a server dependency."""
    lines = [f"bia_loop_lag_seconds {snapshot.loop_lag_seconds:g}"]
    for family, values in (("queue", snapshot.queues), ("task", snapshot.tasks),
                           ("skill", snapshot.skills)):
        for label, value in sorted(values.items()):
            lines.append(f'bia_{family}_total{{state="{label}"}} {value}')
    lines.extend((f"bia_model_requests_total {snapshot.model_requests}",
                  f"bia_model_tokens_total {snapshot.model_tokens}",
                  f"bia_model_cost_minor_total {snapshot.model_cost_minor}",
                  f"bia_model_cache_hits_total {snapshot.model_cache_hits}",
                  f"bia_side_effect_deliveries_total {snapshot.side_effect_deliveries}",
                  f"bia_duplicate_side_effects_total {snapshot.duplicate_side_effects}"))
    return "\n".join(lines) + "\n"
