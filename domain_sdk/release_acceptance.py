"""P08 packaging and independent-domain release acceptance helpers."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from active_agent_platform.diagnostics import HealthService
from active_agent_platform.foundation import FakeClock, SystemClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.contracts import DomainPlugin, JsonValue
from domain_sdk.registry import PluginCatalog


@dataclass(frozen=True, slots=True)
class PlatformReleaseReport:
    status: str
    package_checks: tuple[str, ...]
    virtual_checkpoints: int
    real_checkpoints: int
    readiness_failures: int
    deterministic_replays: int
    errors: tuple[str, ...]
    t06_status: str = "UNKNOWN"
    release_decision: str = "BLOCKED"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_distribution_manifests(root: Path) -> tuple[str, ...]:
    expected = {
        "kernel": ("brainagent-kernel", "brain_kernel*", ()),
        "platform": ("brainagent-platform", "active_agent_platform*", ("brainagent-kernel",)),
        "domain-sdk": ("brainagent-domain-sdk", "domain_sdk*", ("brainagent-kernel", "brainagent-platform")),
    }
    checks: list[str] = []
    for folder, (name, package, dependencies) in expected.items():
        document = tomllib.loads((root / folder / "pyproject.toml").read_text())
        if document["project"]["name"] != name:
            raise ValueError(f"distribution name mismatch: {folder}")
        includes = document["tool"]["setuptools"]["packages"]["find"]["include"]
        if includes != [package]:
            raise ValueError(f"distribution package boundary mismatch: {folder}")
        declared = tuple(document["project"].get("dependencies", ()))
        if any(not any(item.startswith(required) for item in declared) for required in dependencies):
            raise ValueError(f"distribution dependency missing: {folder}")
        checks.append(f"{name}:BOUNDARY_OK")
    return tuple(checks)


async def validate_independent_domain(
    database_path: Path, plugin: DomainPlugin, *,
    invoke: Callable[[Mapping[str, JsonValue]], Awaitable[Mapping[str, JsonValue]]],
    virtual_days: int = 30, real_seconds: float = 0.1,
) -> PlatformReleaseReport:
    if virtual_days < 1 or real_seconds <= 0:
        raise ValueError("validation durations must be positive")
    catalog = PluginCatalog.from_plugins((plugin,))
    if not catalog.plugin_ids:
        raise ValueError("independent domain registered no plugin")
    database = SQLiteDatabase(database_path)
    await database.initialize()
    clock = FakeClock(datetime(2026, 8, 18, tzinfo=UTC))
    failures = replays = 0
    errors: list[str] = []
    for day in range(virtual_days):
        failures += int(not (await HealthService(database, clock).check()).ready)
        first = await invoke({"text": f"research evidence day {day}"})
        second = await invoke({"text": f"research evidence day {day}"})
        if first == second:
            replays += 1
        else:
            errors.append(f"day {day}: PURE skill output changed on replay")
        clock.advance(timedelta(days=1).total_seconds())
    real_clock = SystemClock()
    deadline = real_clock.monotonic() + real_seconds
    real = 0
    while real_clock.monotonic() < deadline:
        failures += int(not (await HealthService(database, real_clock).check()).ready)
        real += 1
        await real_clock.sleep(min(0.02, max(0.0, deadline - real_clock.monotonic())))
    await database.close()
    status = "PASSED" if not errors and failures == 0 and replays == virtual_days else "FAILED"
    return PlatformReleaseReport(status, (), virtual_days, real, failures, replays, tuple(errors))


def write_report(path: Path, report: PlatformReleaseReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
