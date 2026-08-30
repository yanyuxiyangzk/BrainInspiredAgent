from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from apps.quant_agent import runtime as runtime_module
from apps.quant_agent.cli import main, run
from apps.quant_agent.commands import command_help
from apps.quant_agent.shell import (
    COMMANDS,
    slash_arguments,
)
from apps.quant_agent.tui import SlashCompleter, menu_viewport


def test_menu_viewport_scrolls_to_keep_selection_visible() -> None:
    assert menu_viewport(total=25, index=0, offset=0, visible=10) == 0
    assert menu_viewport(total=25, index=12, offset=0, visible=10) == 3
    assert menu_viewport(total=25, index=24, offset=0, visible=10) == 15
    assert menu_viewport(total=25, index=3, offset=15, visible=10) == 3
    assert menu_viewport(total=5, index=4, offset=0, visible=10) == 0


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


def test_welcome_panel_reflects_model_state() -> None:
    from active_agent_platform.foundation import Settings
    from apps.quant_agent.shell import welcome_panel

    assert "未配置 · 输入 /model 选择模型" in welcome_panel(Settings())
    configured = welcome_panel(Settings(
        model_provider="glm",
        model_url="https://open.bigmodel.cn/api/paas/v4",
        model_name="glm-4-flash",
        model_api_key="sk-x",
    ))
    assert "glm-4-flash · glm · 已配置 ✓" in configured
    assert "/help 命令总览 · /model 切换模型" in configured
    assert "缺少 API Key" in welcome_panel(Settings(model_url="https://x/v1", model_name="m"))


def test_model_label_summarizes_state() -> None:
    from active_agent_platform.foundation import Settings
    from apps.quant_agent.shell import model_label

    assert model_label(Settings()) == "未配置"
    assert model_label(Settings(model_url="https://x/v1", model_name="m")) == "m（缺 Key）"
    assert model_label(Settings(
        model_provider="glm", model_url="https://x/v1",
        model_name="glm-4-flash", model_api_key="k",
    )) == "glm-4-flash"
    assert model_label(Settings(
        model_provider="ollama", model_url="http://localhost:11434/v1", model_name="llama3.2",
    )) == "llama3.2"


@pytest.mark.asyncio
async def test_interactive_shell_starts_help_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_builder = runtime_module.build_quant_runtime
    future_review = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)).time()
    monkeypatch.setattr(
        runtime_module, "build_quant_runtime",
        lambda path: real_builder(path, schedule=runtime_module.DailyReviewSchedule(
            at=future_review,
        )),
    )
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
    assert "输入 /model 选择模型" in output and "/model 切换模型" in output
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


@pytest.mark.asyncio
async def test_shell_plain_text_chats_with_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from active_agent_platform.llm import FakeChatModel
    from active_agent_platform.llm_runtime import GovernedLlmClient, LlmConfig

    def fake_client(settings: object) -> GovernedLlmClient:
        del settings
        return GovernedLlmClient(
            FakeChatModel(
                ["你好，我是测试回复。", "第二条回复。"],
                provider="glm", model="glm-4-flash",
            ),
            LlmConfig(provider="glm", model="glm-4-flash", api_key_ref="test",
                      timeout_seconds=5, daily_token_budget=1000),
        )

    monkeypatch.setattr("apps.quant_agent.shell.build_chat_client", fake_client)
    stdin = StringIO("你好\n/clear\n再来一条\n/exit\n")
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0 and not stderr.getvalue()
    output = stdout.getvalue()
    assert "你好，我是测试回复。" in output and "第二条回复。" in output
    assert "助手>" not in output
    assert "— glm-4-flash" in output and "已清空对话上下文" in output


@pytest.mark.asyncio
async def test_shell_chat_without_model_guides_to_setup(tmp_path: Path) -> None:
    stdin = StringIO("你好\n/exit\n")
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0
    assert "还没有配置模型" in stderr.getvalue()
    assert "Unknown command" not in stderr.getvalue()


@pytest.mark.asyncio
async def test_shell_chat_maps_llm_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from active_agent_platform.llm import FakeChatModel, LlmError, LlmErrorCode
    from active_agent_platform.llm_runtime import GovernedLlmClient, LlmConfig

    def failing_client(settings: object) -> GovernedLlmClient:
        del settings
        return GovernedLlmClient(
            FakeChatModel([LlmError(LlmErrorCode.AUTHENTICATION, "bad key")]),
            LlmConfig(provider="glm", model="glm-4-flash", api_key_ref="test",
                      timeout_seconds=5, daily_token_budget=1000),
        )

    monkeypatch.setattr("apps.quant_agent.shell.build_chat_client", failing_client)
    stdin = StringIO("你好\n/exit\n")
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0
    assert "模型认证失败" in stderr.getvalue() and "/model" in stderr.getvalue()


@pytest.mark.asyncio
async def test_shell_img_command_attaches_clipboard_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from active_agent_platform.llm import FakeChatModel
    from active_agent_platform.llm_runtime import GovernedLlmClient, LlmConfig

    image = tmp_path / "paste.png"
    image.write_bytes(b"\x89PNG-fake")
    models: list[FakeChatModel] = []

    def fake_client(settings: object) -> GovernedLlmClient:
        del settings
        model = FakeChatModel(["看到了图片"], provider="glm", model="glm-5.3-flash")
        models.append(model)
        return GovernedLlmClient(
            model,
            LlmConfig(provider="glm", model="glm-5.3-flash", api_key_ref="test",
                      timeout_seconds=5, daily_token_budget=1000),
        )

    monkeypatch.setattr("apps.quant_agent.shell.build_chat_client", fake_client)
    monkeypatch.setattr(
        "apps.quant_agent.shell.capture_clipboard", lambda: ("image", str(image)),
    )
    stdin = StringIO("/img\n图里有什么\n/exit\n")
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0 and not stderr.getvalue()
    output = stdout.getvalue()
    assert "已添加 图片#1" in output and "[已附带 1 张图片]" in output
    assert "看到了图片" in output
    sent = models[0].requests[0].messages[-1]
    assert sent.images and sent.images[0].startswith("data:image/png;base64,")


def test_bare_main_enters_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "argv", ["bia", "--database", str(tmp_path / "main.db")])
    monkeypatch.setattr(sys, "stdin", StringIO("/exit\n"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    assert main() == 0
    assert "输入 /model 选择模型" in stdout.getvalue()
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


@pytest.mark.asyncio
async def test_shell_model_command_persists_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    stdin = StringIO("/model\n1\n1\n\nsk-shell\n/exit\n")
    stdout, stderr = StringIO(), StringIO()
    code = await run(
        ("--database", str(tmp_path / "bia.db"), "shell"), stdout, stderr, stdin,
    )
    assert code == 0
    output = stdout.getvalue()
    assert "当前模型" in output and "选择 Provider" in output and "✓ 模型配置已保存" in output
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "BIA_MODEL_PROVIDER=glm" in env and "BIA_MODEL_API_KEY=sk-shell" in env
    assert "BIA_MODEL_NAME=glm-5.3-flash" in env
