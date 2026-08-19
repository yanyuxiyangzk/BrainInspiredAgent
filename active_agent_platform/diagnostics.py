"""I03 side-effect-free liveness, readiness, dependency and brain diagnostics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from active_agent_platform.metrics import MetricsSnapshot, PlatformMetrics
from active_agent_platform.state import BrainState
from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock


class ProbeStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class HealthReport:
    captured_at: datetime
    liveness: ProbeStatus
    readiness: ProbeStatus
    dependencies: Mapping[str, ProbeStatus]
    brain: ProbeStatus
    brain_state: Mapping[str, object] | None
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.readiness is ProbeStatus.HEALTHY

    def to_dict(self) -> dict[str, object]:
        return {"captured_at": _time(self.captured_at), "liveness": self.liveness,
                "readiness": self.readiness, "ready": self.ready,
                "dependencies": dict(self.dependencies), "brain": self.brain,
                "brain_state": self.brain_state, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    health: HealthReport
    metrics: MetricsSnapshot | None
    migrations: tuple[str, ...]
    overdue_tasks: tuple[Mapping[str, object], ...]
    recent_errors: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {"health": self.health.to_dict(),
                "metrics": None if self.metrics is None else self.metrics.to_dict(),
                "migrations": list(self.migrations),
                "overdue_tasks": [dict(item) for item in self.overdue_tasks],
                "recent_errors": [dict(item) for item in self.recent_errors]}


class HealthService:
    def __init__(
        self,
        database: SQLiteDatabase,
        clock: Clock,
        *,
        metrics: PlatformMetrics | None = None,
        brain_state: Callable[[], BrainState] | None = None,
        queue_warning_threshold: int = 100,
    ) -> None:
        if queue_warning_threshold < 1:
            raise ValueError("queue warning threshold must be positive")
        self._database, self._clock = database, clock
        self._metrics = metrics or PlatformMetrics(database, clock)
        self._brain_state = brain_state
        self._queue_warning_threshold = queue_warning_threshold

    async def check(self) -> HealthReport:
        now = self._clock.now().astimezone(UTC)
        reasons: list[str] = []
        try:
            integrity = await self._database.fetch_one("PRAGMA quick_check")
            migrations = await self._database.fetch_one("SELECT count(*) AS value FROM schema_migration")
            database_ok = integrity is not None and integrity[0] == "ok" and migrations is not None
            pending = await self._database.fetch_one(
                "SELECT count(*) AS value FROM outbox_event WHERE publish_state = 'PENDING'"
            )
            backlog = 0 if pending is None else int(pending["value"])
        except Exception as error:  # noqa: BLE001 - health boundary must return a report
            database_ok, backlog = False, 0
            reasons.append(f"sqlite unavailable: {type(error).__name__}")
        dependencies = {"sqlite": ProbeStatus.HEALTHY if database_ok else ProbeStatus.UNHEALTHY,
                        "outbox": ProbeStatus.HEALTHY}
        if database_ok and backlog >= self._queue_warning_threshold:
            dependencies["outbox"] = ProbeStatus.DEGRADED
            reasons.append(f"outbox backlog is {backlog}")
        elif not database_ok:
            dependencies["outbox"] = ProbeStatus.UNKNOWN
        brain_value = self._brain_state() if self._brain_state is not None else None
        brain = ProbeStatus.UNKNOWN if brain_value is None else (
            ProbeStatus.DEGRADED if brain_value.brain_mode.value in {"DEGRADED", "SAFE"}
            else ProbeStatus.HEALTHY
        )
        if brain is ProbeStatus.DEGRADED:
            reasons.append(f"brain mode is {brain_value.brain_mode.value}")  # type: ignore[union-attr]
        ready = ProbeStatus.HEALTHY if database_ok and dependencies["outbox"] is not ProbeStatus.DEGRADED \
            else ProbeStatus.UNHEALTHY if not database_ok else ProbeStatus.DEGRADED
        return HealthReport(
            now, ProbeStatus.HEALTHY, ready, MappingProxyType(dependencies), brain,
            None if brain_value is None else MappingProxyType(brain_value.to_dict()), tuple(reasons),
        )

    async def diagnose(self, *, recent_limit: int = 20) -> DiagnosticSnapshot:
        if not 1 <= recent_limit <= 100:
            raise ValueError("recent_limit must be between 1 and 100")
        health = await self.check()
        try:
            metrics = await self._metrics.snapshot()
            migrations = tuple(str(row["version"]) for row in await self._database.fetch_all(
                "SELECT version FROM schema_migration ORDER BY version"
            ))
            overdue = tuple(dict(row) for row in await self._database.fetch_all(
                """SELECT task_id,status,deadline,correlation_id FROM task
                   WHERE status NOT IN ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED','EXPIRED','REQUIRES_REVIEW')
                     AND deadline < ? ORDER BY deadline LIMIT ?""",
                (_time(self._clock.now()), recent_limit),
            ))
            errors = tuple(dict(row) for row in await self._database.fetch_all(
                """SELECT task_id,status,error_id,correlation_id FROM task
                   WHERE error_id IS NOT NULL ORDER BY rowid DESC LIMIT ?""", (recent_limit,)
            ))
        except Exception:  # noqa: BLE001 - partial diagnostics remain useful
            metrics, migrations, overdue, errors = None, (), (), ()
        return DiagnosticSnapshot(health, metrics, migrations, overdue, errors)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
