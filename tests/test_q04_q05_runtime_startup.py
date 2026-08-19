from __future__ import annotations

import json
from datetime import time
from io import StringIO
from pathlib import Path

import pytest

from apps.quant_agent.cli import EXIT_OK, EXIT_UNAVAILABLE, run
from apps.quant_agent.runtime import DailyReviewSchedule, build_quant_runtime
from apps.quant_agent.startup import StartupPathError, _prepare_directory, prepare_runtime_paths


@pytest.mark.asyncio
async def test_q04_scheduler_runs_daily_review_once_and_restart_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "daily-runtime.db"
    first = build_quant_runtime(path)
    now = first.service._clock.now()
    await first.database.close()
    schedule = DailyReviewSchedule(
        at=time(now.hour, now.minute, now.second),
        timezone="UTC", window_seconds=60, trading_days_only=False,
    )
    components = build_quant_runtime(path, schedule=schedule)
    await components.database.initialize()
    await components.service.start()
    assert (await components.service._scheduler.tick()).triggered == 1
    assert (await components.service._relay.publish_due()).published == 1
    row = await components.database.fetch_one(
        "SELECT review_key,status FROM rest_repair_run"
    )
    assert row is not None
    assert tuple(row) == (f"daily_review:{now.date().isoformat()}", "SUCCEEDED")
    await components.service.stop()
    await components.database.close()

    restarted = build_quant_runtime(path, schedule=schedule)
    await restarted.database.initialize()
    await restarted.service.start()
    assert (await restarted.service._scheduler.tick()).triggered == 0
    counts = await restarted.database.fetch_one(
        "SELECT (SELECT count(*) FROM rest_repair_run),"
        "(SELECT count(*) FROM workflow_run)"
    )
    assert counts is not None and tuple(counts) == (1, 1)
    await restarted.service.stop()
    await restarted.database.close()


@pytest.mark.asyncio
async def test_q05_cold_start_creates_nested_parent(tmp_path: Path) -> None:
    path = tmp_path / "new" / "nested" / "bia.db"
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", str(path), "start"), stdout, stderr)
    assert code == EXIT_OK and not stderr.getvalue()
    assert path.is_file()
    assert json.loads(stdout.getvalue())["status"] == "READY"


@pytest.mark.asyncio
async def test_q05_readonly_parent_fails_before_database_creation(tmp_path: Path) -> None:
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o555)
    path = parent / "bia.db"
    stdout, stderr = StringIO(), StringIO()
    try:
        code = await run(("--database", str(path), "start"), stdout, stderr)
    finally:
        parent.chmod(0o755)
    assert code == EXIT_UNAVAILABLE
    assert json.loads(stderr.getvalue())["error"]["code"] == "STARTUP_PATH_INVALID"
    assert not path.exists()


@pytest.mark.asyncio
async def test_q05_artifact_collision_fails_before_database_creation(tmp_path: Path) -> None:
    (tmp_path / "bia-artifacts").write_text("collision", encoding="utf-8")
    path = tmp_path / "bia.db"
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", str(path), "start"), stdout, stderr)
    assert code == EXIT_UNAVAILABLE
    assert "artifact path" in stderr.getvalue()
    assert not path.exists()


def test_q05_rejects_database_directory_and_readonly_files(tmp_path: Path) -> None:
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    with pytest.raises(StartupPathError, match="regular file"):
        prepare_runtime_paths(database_directory)

    database = tmp_path / "readonly.db"
    database.touch(mode=0o444)
    with pytest.raises(StartupPathError, match="database file"):
        prepare_runtime_paths(database)

    database.chmod(0o644)
    artifacts = tmp_path / "readonly-artifacts"
    artifacts.mkdir(mode=0o555)
    try:
        with pytest.raises(StartupPathError, match="artifact directory"):
            prepare_runtime_paths(database)
    finally:
        artifacts.chmod(0o755)


def test_q05_wraps_parent_creation_and_access_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_file = tmp_path / "parent-file"
    parent_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StartupPathError, match="cannot create"):
        prepare_runtime_paths(parent_file / "bia.db")

    directory = tmp_path / "inaccessible"
    directory.mkdir()
    monkeypatch.setattr("apps.quant_agent.startup.os.access", lambda *_: False)
    with pytest.raises(StartupPathError, match="not accessible"):
        _prepare_directory(directory, "test directory")


@pytest.mark.asyncio
async def test_q04_invalid_runtime_schedule_is_structured(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "run", "--daily-review-at", "25:00"),
        stdout, stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["error"]["code"] == "RUNTIME_CONFIG_INVALID"
