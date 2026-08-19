from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from apps.quant_agent.cli import EXIT_OK, EXIT_USAGE, _plain, _render, run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ("start", "status", "health", "diagnose", "stop", "log")
)
async def test_basic_cli_commands_are_machine_readable(tmp_path: Path, command: str) -> None:
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", str(tmp_path / "cli.db"), command), stdout, stderr)
    assert code == EXIT_OK and stderr.getvalue() == ""
    assert isinstance(json.loads(stdout.getvalue()), dict)


@pytest.mark.asyncio
async def test_trace_cli_and_argument_errors(tmp_path: Path) -> None:
    database = str(tmp_path / "cli.db")
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "replay", "corr"), stdout, stderr) == EXIT_OK
    assert json.loads(stdout.getvalue())["correlation_id"] == "corr"

    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "log", "--limit", "0"), stdout, stderr) == EXIT_USAGE
    assert "INVALID_ARGUMENT" in stderr.getvalue()

    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "insights", "latest", "--limit", "0"), stdout, stderr) == EXIT_USAGE

    stdout, stderr = StringIO(), StringIO()
    assert await run(("--unknown",), stdout, stderr) == EXIT_USAGE


@pytest.mark.asyncio
async def test_governed_inject_market_command_and_subscription_cli(tmp_path: Path) -> None:
    database = str(tmp_path / "commands.db")
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", database, "inject", "status",
                      "--idempotency-key", "status:1"), stdout, stderr)
    assert code == EXIT_OK and json.loads(stdout.getvalue())["governed"] is True
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", database, "market", "summary", "--trade-date", "2026-08-18",
                      "--symbols", "INDEX.TEST"), stdout, stderr)
    assert code == EXIT_OK and json.loads(stdout.getvalue())["command"] == "market.summary"
    stdout, stderr = StringIO(), StringIO()
    code = await run(("--database", database, "inject", "unknown"), stdout, stderr)
    assert code == EXIT_USAGE and "COMMAND_NOT_ALLOWED" in stderr.getvalue()
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "subscriptions", "add", "me",
                      "--hourly-limit", "2"), stdout, stderr) == EXIT_OK
    assert json.loads(stdout.getvalue())["status"] == "SUBSCRIBED"
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "subscriptions", "deliver", "me", "insight-1"),
                     stdout, stderr) == EXIT_OK
    delivery_id = json.loads(stdout.getvalue())["delivery_id"]
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "subscriptions", "list", "me"),
                     stdout, stderr) == EXIT_OK
    assert json.loads(stdout.getvalue())["deliveries"][0]["delivery_id"] == delivery_id
    stdout, stderr = StringIO(), StringIO()
    assert await run(("--database", database, "subscriptions", "read", delivery_id),
                     stdout, stderr) == EXIT_OK
    assert json.loads(stdout.getvalue())["status"] == "READ"


def test_markdown_fallback_and_empty_insights() -> None:
    assert _render({"status": "ok"}, "markdown").startswith("```json")
    assert _render({"insights": []}, "markdown") == "_No insights._"
    document = {
        "insight_id": "i", "title": "T", "summary": "S", "fresh_until": "now",
        "stale": False, "workflow_version": "1", "correlation_id": "c", "evidence": None,
    }
    assert _render(document, "markdown").startswith("# T")
    assert _plain(({"nested": (1,)},)) == [{"nested": [1]}]
