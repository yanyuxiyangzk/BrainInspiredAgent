from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from apps.quant_agent.cli import main, run
from apps.quant_agent.commands import command_help
from apps.quant_agent.shell import (
    COMMANDS,
    SlashCompleter,
    _align_completion_menu,
    _find_floats,
    slash_arguments,
)


def test_slash_command_aliases() -> None:
    assert "/help" in COMMANDS and "/health" in COMMANDS
    assert slash_arguments("/market INDEX.A,INDEX.B --title 'Daily view'") == (
        "market", "summary", "--symbols", "INDEX.A,INDEX.B", "--title", "Daily view",
    )
    assert slash_arguments("/market summary --symbols INDEX.A") == (
        "market", "summary", "--symbols", "INDEX.A",
    )
    assert slash_arguments("/insights") == ("insights", "latest")
    assert slash_arguments("/subscribe") == ("subscriptions", "add", "local-user")
    assert slash_arguments("/deliveries me") == ("subscriptions", "list", "me")
    assert slash_arguments("/trace correlation") == ("replay", "correlation")
    assert slash_arguments("/loop") == ("loop", "status")
    assert slash_arguments("/loop lag") == ("loop", "lag")
    assert slash_arguments("/") is None
    assert slash_arguments("plain text") is None
    assert slash_arguments("/unknown") is None
    assert "inspect the active LoopEngine" in command_help("loop")
    with pytest.raises(KeyError):
        command_help("missing")


def test_live_completion_filters_as_the_user_types() -> None:
    completer = SlashCompleter()
    event = CompleteEvent(completion_requested=False)
    assert [item.text for item in completer.get_completions(Document("/"), event)] == list(COMMANDS)
    assert [item.text for item in completer.get_completions(Document("/h"), event)] == [
        "/health", "/help",
    ]
    assert not list(completer.get_completions(Document("/market "), event))


def test_completion_menu_is_anchored_under_slash() -> None:
    from prompt_toolkit import PromptSession

    session: PromptSession[str] = PromptSession()
    _align_completion_menu(session)
    floats = _find_floats(session.app.layout.container)
    assert all(not item.xcursor and item.left == 4 for item in floats[:2])


@pytest.mark.asyncio
async def test_interactive_shell_starts_help_and_stops(tmp_path: Path) -> None:
    stdin = StringIO(
        "/help\n/help loop\n/loop status\n/loop services\n/loop lag\n"
        "/loop checkpoints\n/exit\n"
    )
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "shell" / "bia.db"), "shell"),
        stdout, stderr, stdin,
    )
    assert code == 0 and not stderr.getvalue()
    output = stdout.getvalue()
    assert "╭───────╮     ╭───────╮" in output and "COGNITIVE AGENT" in output
    assert "╭──╮ ╷ ╭──╮" in output and "├──┤ │ ├──┤" in output
    assert output.count("●") >= 18 and "○" not in output and "🧠" not in output
    assert "BIA terminal ready" in output
    assert "/market" in output
    assert "LoopEngine HEALTHY" in output and "quant_runtime" in output
    assert "commands=0 outbox=0" in output and "No schedule checkpoints" in output
    assert "BIA terminal stopped" in output


@pytest.mark.asyncio
async def test_interactive_shell_dispatches_commands_and_reports_input_errors(
    tmp_path: Path,
) -> None:
    stdin = StringIO(
        "\nplain text\n/unknown\n/'\n/h\n/market INDEX.SHELL\n/commands\n"
        "/insights\n/health\n/trace missing\n/exit\n"
    )
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0
    assert "PUBLISHED" in stdout.getvalue()
    assert "HEALTHY" in stdout.getvalue()
    assert "Matches: /help  /health" in stdout.getvalue() or "Matches: /health  /help" in stdout.getvalue()
    assert "Unknown command" in stderr.getvalue()
    assert "Invalid command" in stderr.getvalue()


@pytest.mark.asyncio
async def test_interactive_shell_reports_startup_failure(tmp_path: Path) -> None:
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(database_directory), "shell"), stdout, stderr, StringIO("/exit\n"),
    )
    assert code == 5
    assert "Unable to start BIA" in stderr.getvalue()


@pytest.mark.asyncio
async def test_loop_cli_does_not_invent_cross_process_health(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "loop-query.db"), "loop", "services"),
        stdout, stderr,
    )
    assert code == 0 and not stderr.getvalue()
    value = json.loads(stdout.getvalue())
    assert value["status"] == "UNKNOWN" and value["scope"] == "services"


@pytest.mark.asyncio
async def test_shell_reports_unknown_help_and_lists_checkpoint(tmp_path: Path) -> None:
    from active_agent_platform.storage import SQLiteDatabase

    path = tmp_path / "checkpoint.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO schedule_checkpoint(schedule_id,occurrence_key,status,consumed_at) "
            "VALUES ('daily','2026-08-20','FIRED','2026-08-20T00:00:00Z')"
        )
    await database.close()
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(path), "shell"), stdout, stderr,
        StringIO("/help missing\n/loop checkpoints\n/exit\n"),
    )
    assert code == 0 and "Unknown command: missing" in stderr.getvalue()
    assert "daily 2026-08-20 FIRED" in stdout.getvalue()


def test_bare_main_enters_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "argv", ["bia", "--database", str(tmp_path / "main.db")])
    monkeypatch.setattr(sys, "stdin", StringIO("/exit\n"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    assert main() == 0
    assert "BIA terminal ready" in stdout.getvalue()
    assert not stderr.getvalue()


def test_main_handles_ctrl_c_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "argv", ["bia"])
    monkeypatch.setattr(sys, "stdin", StringIO())
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    def interrupt(coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]
        raise KeyboardInterrupt

    monkeypatch.setattr("apps.quant_agent.cli.asyncio.run", interrupt)
    assert main() == 130
    assert "BIA terminal stopped" in stdout.getvalue()
    assert not stderr.getvalue()
