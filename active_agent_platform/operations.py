"""Domain-neutral operational facade for health, metrics, migrations and traces."""

from __future__ import annotations

from dataclasses import dataclass

from active_agent_platform.diagnostics import DiagnosticSnapshot, HealthReport, HealthService
from active_agent_platform.metrics import MetricsSnapshot, PlatformMetrics
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.trace import TraceBundle, TraceQuery
from brain_kernel.ports import Clock


@dataclass(frozen=True, slots=True)
class OperationsSnapshot:
    health: HealthReport
    metrics: MetricsSnapshot
    migrations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "health": self.health.to_dict(),
            "metrics": self.metrics.to_dict(),
            "migrations": list(self.migrations),
        }


class PlatformOperations:
    """Read-only operations API that is independent of any domain application."""

    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database = database
        self._metrics = PlatformMetrics(database, clock)
        self._health = HealthService(database, clock, metrics=self._metrics)
        self._trace = TraceQuery(database)

    async def snapshot(self) -> OperationsSnapshot:
        health = await self._health.check()
        metrics = await self._metrics.snapshot()
        rows = await self._database.fetch_all(
            "SELECT version FROM schema_migration ORDER BY version"
        )
        return OperationsSnapshot(health, metrics, tuple(str(row["version"]) for row in rows))

    async def diagnose(self, *, recent_limit: int = 20) -> DiagnosticSnapshot:
        return await self._health.diagnose(recent_limit=recent_limit)

    async def trace(self, correlation_id: str) -> TraceBundle:
        return await self._trace.by_correlation(correlation_id)
