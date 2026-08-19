"""T06 virtual-day and wall-clock soak validation with checkpoint reports."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from active_agent_platform.diagnostics import HealthService, ProbeStatus
from active_agent_platform.foundation import FakeClock, SystemClock
from active_agent_platform.metrics import PlatformMetrics
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.delivery import InsightDeliveryService


@dataclass(frozen=True, slots=True)
class SoakReport:
    mode: str
    started_at: str
    finished_at: str | None
    requested_seconds: float
    checkpoints: int
    readiness_failures: int
    max_loop_lag_seconds: float
    duplicate_side_effects: int
    errors: tuple[str, ...]
    status: str
    process_id: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


async def virtual_30_days(database_path: Path, output: Path) -> SoakReport:
    start = datetime(2026, 8, 18, tzinfo=UTC)
    clock = FakeClock(start)
    database = SQLiteDatabase(database_path)
    await database.initialize()
    delivery = InsightDeliveryService(database, clock)
    await delivery.subscribe("t06-soak", hourly_limit=100)
    failures = duplicates = 0
    errors: list[str] = []
    for day in range(30):
        result = await delivery.deliver("t06-soak", f"virtual-day-{day}")
        repeated = await delivery.deliver("t06-soak", f"virtual-day-{day}")
        duplicates += int(repeated.status == "DUPLICATE")
        if result.status != "DELIVERED" or repeated.status != "DUPLICATE":
            errors.append(f"day {day}: delivery={result.status}, replay={repeated.status}")
        health = await HealthService(database, clock).check()
        failures += int(health.readiness is not ProbeStatus.HEALTHY)
        clock.advance(timedelta(days=1).total_seconds())
    rows = await delivery.deliveries("t06-soak")
    if len(rows) != 30:
        errors.append(f"expected 30 durable deliveries, found {len(rows)}")
    report = SoakReport(
        "virtual-30-days", _time(start), _time(clock.now()), 30 * 86400, 30,
        failures, 0.0, duplicates, tuple(errors),
        "PASSED" if not errors and failures == 0 and duplicates == 30 else "FAILED", os.getpid(),
    )
    _write(output, report)
    await database.close()
    return report


async def real_soak(
    database_path: Path, output: Path, *, duration_seconds: float = 86400,
    interval_seconds: float = 60,
) -> SoakReport:
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("soak duration and interval must be positive")
    clock = SystemClock()
    started = clock.now()
    database = SQLiteDatabase(database_path)
    await database.initialize()
    metrics = PlatformMetrics(database, clock)
    health = HealthService(database, clock, metrics=metrics)
    deadline = clock.monotonic() + duration_seconds
    checkpoints = failures = 0
    max_lag = 0.0
    errors: list[str] = []
    report = SoakReport("real-24h", _time(started), None, duration_seconds, 0, 0, 0.0, 0,
                        (), "RUNNING", os.getpid())
    _write(output, report)
    try:
        while True:
            expected = clock.now()
            checked = await health.check()
            failures += int(checked.readiness is not ProbeStatus.HEALTHY)
            checkpoints += 1
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                break
            await clock.sleep(min(interval_seconds, remaining))
            metrics.observe_loop(expected + timedelta(seconds=min(interval_seconds, remaining)))
            snapshot = await metrics.snapshot()
            max_lag = max(max_lag, snapshot.loop_lag_seconds)
            _write(output, SoakReport(
                "real-24h", _time(started), None, duration_seconds, checkpoints, failures,
                max_lag, snapshot.duplicate_side_effects, tuple(errors), "RUNNING", os.getpid(),
            ))
    except Exception as error:  # noqa: BLE001 - persist soak failure evidence
        errors.append(f"{type(error).__name__}: {error}")
    finished = clock.now()
    report = SoakReport(
        "real-24h", _time(started), _time(finished), duration_seconds, checkpoints,
        failures, max_lag, 0, tuple(errors), "PASSED" if not errors and failures == 0 else "FAILED",
        os.getpid(),
    )
    _write(output, report)
    await database.close()
    return report


def _write(path: Path, report: SoakReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
