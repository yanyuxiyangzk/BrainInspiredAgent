"""Run the Q08 external CLI/runtime recovery and idempotency release gate."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commands", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--test-count", type=int)
    parser.add_argument("--coverage-percent", type=float)
    args = parser.parse_args()
    if args.commands < 2 or args.timeout <= 0:
        parser.error("commands must be at least 2 and timeout must be positive")
    report = validate(args.database, args.commands, args.timeout)
    if args.test_count is not None:
        report["automated_tests"] = args.test_count
    if args.coverage_percent is not None:
        report["coverage_percent"] = args.coverage_percent
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


def validate(database: Path, command_count: int, timeout: float) -> dict[str, object]:
    started = datetime.now(UTC)
    errors: list[str] = []
    database.parent.mkdir(parents=True, exist_ok=True)
    _cli(database, "start")
    _cli(database, "subscriptions", "add", "q08-release", "--hourly-limit", str(command_count * 2))
    process = _start(database)
    ids: list[str] = []
    split = command_count // 2
    try:
        for index in range(split):
            ids.append(_submit(database, index))
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        strong_kill_recovered = process.returncode == -signal.SIGKILL

        process = _start(database)
        for index in range(split):
            _submit(database, index)  # same business keys must not execute twice
        for index in range(split, command_count):
            ids.append(_submit(database, index))
        statuses = _wait(database, ids, timeout)
    except Exception as error:  # noqa: BLE001 - release report must preserve the failure
        errors.append(f"{type(error).__name__}: {error}")
        statuses = {}
        strong_kill_recovered = False
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    counts = _counts(database)
    failed = sum(status != "SUCCEEDED" for status in statuses.values())
    expected = {
        "command_execution": command_count,
        "market_task": command_count,
        "market_workflow_run": command_count,
        "market_episode": command_count,
        "local_notification_delivery": command_count,
        "insight_delivery": command_count,
    }
    for name, value in expected.items():
        if counts.get(name) != value:
            errors.append(f"{name}: expected {value}, found {counts.get(name)}")
    if len(statuses) != command_count or failed:
        errors.append(f"terminal commands: expected {command_count} successes, found {len(statuses) - failed}")
    if not strong_kill_recovered:
        errors.append("runtime did not demonstrate SIGKILL recovery")
    finished = datetime.now(UTC)
    return {
        "gate": "Q08",
        "started_at": _stamp(started),
        "finished_at": _stamp(finished),
        "status": "PASSED" if not errors else "FAILED",
        "release_decision": "RELEASABLE" if not errors else "BLOCKED",
        "requested_commands": command_count,
        "successful_commands": len(statuses) - failed,
        "failed_commands": failed,
        "strong_kill_recovered": strong_kill_recovered,
        "duplicate_side_effects": sum(abs(counts.get(name, 0) - value) for name, value in expected.items()),
        "counts": counts,
        "errors": errors,
    }


def _start(database: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.quant_agent", "--database", str(database), "run"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if process.stdout is None:
        raise RuntimeError("runtime stdout is unavailable")
    ready = json.loads(process.stdout.readline())
    if ready.get("status") != "READY":
        raise RuntimeError(f"runtime failed readiness: {ready}")
    return process


def _submit(database: Path, index: int) -> str:
    value = _cli(
        database, "market", "summary", "--symbols", f"INDEX.Q08.{index:03d}",
        "--title", f"Q08 release command {index:03d}",
    )
    return str(value["message_id"])


def _wait(database: Path, ids: list[str], timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    pending = set(ids)
    statuses: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for message_id in tuple(pending):
            result = subprocess.run(
                [sys.executable, "-m", "apps.quant_agent", "--database", str(database),
                 "commands", message_id], capture_output=True, text=True,
                check=False,
            )
            if result.returncode == 0:
                status = str(json.loads(result.stdout)["status"])
                if status in {"SUCCEEDED", "FAILED"}:
                    statuses[message_id] = status
                    pending.remove(message_id)
        if pending:
            time.sleep(0.05)
    return statuses


def _cli(database: Path, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "apps.quant_agent", "--database", str(database), *arguments],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _counts(database: Path) -> dict[str, int]:
    queries = {
        "command_execution": "SELECT count(*) FROM command_execution",
        "market_task": """SELECT count(*) FROM task t JOIN workflow_run w
                           ON w.task_id=t.task_id WHERE w.workflow_id='market_summary'""",
        "market_workflow_run":
            "SELECT count(*) FROM workflow_run WHERE workflow_id='market_summary'",
        "market_episode": """SELECT count(*) FROM episode e JOIN workflow_run w
                              ON w.task_id=e.task_id WHERE w.workflow_id='market_summary'""",
        "local_notification_delivery": "SELECT count(*) FROM local_notification_delivery",
        "insight_delivery": "SELECT count(*) FROM insight_delivery",
        "daily_review_run": "SELECT count(*) FROM workflow_run WHERE workflow_id='daily_review'",
    }
    with sqlite3.connect(database) as connection:
        return {name: int(connection.execute(query).fetchone()[0])
                for name, query in queries.items()}


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
