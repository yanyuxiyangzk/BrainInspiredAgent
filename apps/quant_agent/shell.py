"""Interactive slash-command terminal for the local quant agent."""

from __future__ import annotations

import asyncio
import functools
import shlex
import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import LlmError
from apps.quant_agent.chat import (
    ChatInputError,
    ChatSession,
    WhitespaceNormalizer,
    blank_line_separator,
    build_chat_client,
    describe_llm_error,
    extract_images,
    find_missing_images,
    format_footer,
    image_data_url,
    usage_note,
)
from apps.quant_agent.clipboard import capture_clipboard
from apps.quant_agent.commands import COMMANDS, command_help
from apps.quant_agent.model_picker import OLLAMA_PROVIDER, configure_model
from apps.quant_agent.tui import BiaTui

HELP = command_help()

BANNER = r"""
                   ╭───────╮     ╭───────╮
               ╭───╯ ╭───╮ ╰─────╯ ╭───╮ ╰───╮
            ╭──╯ ╭────╯   ╰──╮ ╭──╯   ╰────╮ ╰──╮
          ╭─╯ ╭──╯ ╭─────╮  ╰─┼─╯  ╭─────╮ ╰──╮ ╰─╮
         ╭╯ ╭─╯ ╭──╯  ●  ╰──╮ │ ╭──╯  ●  ╰──╮ ╰─╮ ╰╮
        ╭╯  ╰───╯ ╭─────╮   ╰─┼─╯   ╭─────╮ ╰───╯  ╰╮
        │  ●──╮   ╭╯     ╰──╮  │  ╭──╯     ╰╮   ╭──●  │
        │     ├───┤  ●──╮   ╰──┼──╯   ╭──●  ├───┤     │
        │  ●──╯   ╰╮    │ ╭──╮ ╷ ╭──╮ │    ╭╯   ╰──●  │
        │           ╰──●│ │  │ │ │  │ │●──╯           │
        │  ●──╮         ╰─├──┤ │ ├──┤─╯         ╭──●  │
        │     ├───╮       │  │ │ │  │       ╭───┤     │
        │  ●──╯   ╰──●    ╰──╯ ╵ ╵  ╵    ●──╯   ╰──●  │
        ╰╮   ╭─────╮  ╰───────┼───────╯  ╭─────╮   ╭╯
         ╰─╮ ╰──●  ╰──╮       │       ╭──╯  ●──╯ ╭─╯
           ╰─╮         ╰───────┼───────╯         ╭─╯
             ╰───────╮         │         ╭───────╯
                     ╰─────────┴─────────╯
                         COGNITIVE AGENT
                  类脑 Agent · Cognitive Runtime
          感知 · 记忆 · 推理 · 规划 · 执行 · 复盘
"""

SHELL_STYLE = Style.from_dict({
    "arrow": "bold #38bdf8",
    "rule": "#5c6370",
    "status-left": "#7a828a",
    "status-model": "#e5c07b bold",
    "status-note": "#7dd3fc",
    "thinking": "#7dd3fc",
    "menu-row": "#e2e8f0",
    "menu-selected": "bg:#38bdf8 #08111a bold",
})

@functools.lru_cache(maxsize=1)
def _build_tag() -> str:
    """Short git hash of the running source tree, so builds are distinguishable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except OSError:
        return "dev"
    return result.stdout.strip() or "dev"


def model_label(settings: Settings) -> str:
    """Compact model name for the Codex-style input toolbar."""
    key_ready = bool(settings.model_api_key) or settings.model_provider == OLLAMA_PROVIDER
    if settings.model_url and settings.model_name and key_ready:
        return settings.model_name
    if settings.model_name or settings.model_url:
        return f"{settings.model_name or settings.model_url}（缺 Key）"
    return "未配置"


def welcome_panel(settings: Settings) -> str:
    """Quick-start panel under the banner: current model state and shortcuts."""
    key_ready = bool(settings.model_api_key) or settings.model_provider == OLLAMA_PROVIDER
    if settings.model_url and settings.model_name and key_ready:
        model_line = f"{settings.model_name} · {settings.model_provider} · 已配置 ✓"
    elif settings.model_name or settings.model_url:
        model_line = f"{settings.model_name or settings.model_url} · 缺少 API Key，输入 /model 补充"
    else:
        model_line = "未配置 · 输入 /model 选择模型"
    width = shutil.get_terminal_size(fallback=(80, 24)).columns - 1
    rule = "─" * max(8, width)
    return "\n".join([
        rule,
        f"          模型   {model_line}",
        f"          提示   直接输入与模型对话 · /img 或 Ctrl+V 贴图 · /help 命令总览 · /model 切换模型 · 构建 {_build_tag()}",
        rule,
    ]) + "\n"


async def interactive(
    database_path: Path, stdin: TextIO, stdout: TextIO, stderr: TextIO,
) -> int:
    from apps.quant_agent.cli import EXIT_OK, run
    from apps.quant_agent.runtime import build_quant_runtime
    from apps.quant_agent.startup import prepare_runtime_paths

    try:
        prepare_runtime_paths(database_path)
        components = build_quant_runtime(database_path)
        await components.database.initialize()
    except (OSError, RuntimeError, ValueError) as error:
        stderr.write(f"Unable to start BIA: {error}\n")
        return 5

    serving = asyncio.create_task(components.engine.run(), name="bia-loop-engine")
    await components.engine.wait_started()
    settings = Settings.from_env()
    model_state = {"label": model_label(settings)}
    pending_images: list[str] = []
    image_registry: dict[str, str] = {}
    image_counter = {"n": 0}

    def register_image(path: str) -> str:
        image_counter["n"] += 1
        token = f"图片#{image_counter['n']}"
        image_registry[token] = path
        return token

    def resolve_image_tokens(text: str) -> str:
        for token, path in image_registry.items():
            text = text.replace(f"[{token}]", path)
        return text

    tty = stdin.isatty() and stdout.isatty()
    real_stdout = stdout
    tui: BiaTui | None = None
    sub_session: PromptSession[str] | None = None
    if tty:  # pragma: no cover - real TTY integration
        from apps.quant_agent.tui import QUIT_SENTINEL, TranscriptWriter

        tui = BiaTui(lambda: model_state["label"])
        tui.on_image = register_image
        stdout = cast(TextIO, TranscriptWriter(tui))
        stderr = cast(TextIO, TranscriptWriter(tui))
        sub_session = PromptSession()
        tui.start()
    stdout.write(BANNER)
    stdout.write(welcome_panel(settings))
    stdout.flush()
    async def ask(prompt: str) -> str:
        if sub_session is not None:
            return (await sub_session.prompt_async(prompt)).strip()
        stdout.write(prompt); stdout.flush()
        return (await asyncio.to_thread(stdin.readline)).strip()
    async def ask_secret(prompt: str) -> str:
        if sub_session is not None:
            return (await sub_session.prompt_async(prompt, is_password=True)).strip()
        stdout.write(prompt); stdout.flush()
        return (await asyncio.to_thread(stdin.readline)).strip()
    chat: ChatSession | None = None

    async def chat_turn(text: str) -> None:
        nonlocal chat
        try:
            cleaned, images = extract_images(resolve_image_tokens(text))
            for path in pending_images:
                images = images + (image_data_url(path),)
        except ChatInputError as error:
            stderr.write(f"{error}\n")
            return
        if not cleaned:
            cleaned = "请看这张图片"
        missing = find_missing_images(text)
        if missing:
            stderr.write(
                "⚠ 未找到图片文件：" + "、".join(missing)
                + "（确认路径是否正确，或截图后用 /img 从剪贴板添加）\n"
            )
        if chat is None or chat.label != model_state["label"]:
            client = build_chat_client(Settings.from_env())
            if client is None:
                stderr.write("还没有配置模型：先输入 /model 选择模型，再直接输入文字对话。\n")
                return
            chat = ChatSession(client, label=model_state["label"])
        normalizer = WhitespaceNormalizer()
        emitted = {"chars": 0}

        def print_delta(delta: str) -> None:
            text = normalizer.feed(delta)
            if not emitted["chars"] and not text:
                return
            if not emitted["chars"]:
                if tui is not None:
                    tui.stop_thinking()
                if images:
                    stdout.write(f"[已附带 {len(images)} 张图片]\n")
            emitted["chars"] += len(delta)
            stdout.write(text)
            stdout.flush()

        if tui is not None:
            tui.start_thinking()
        started = time.monotonic()
        try:
            response = await chat.send(cleaned, on_delta=print_delta, images=images)
        except LlmError as error:
            if tui is not None:
                tui.stop_thinking()
            if emitted["chars"]:
                stdout.write("\n")
            stderr.write(describe_llm_error(error) + "\n")
            return
        if tui is not None:
            tui.stop_thinking()
        pending_images.clear()
        tail = normalizer.flush()
        if tail:
            stdout.write(tail)
        seconds = time.monotonic() - started
        if tui is not None:
            tui.set_usage(usage_note(response, seconds))
        else:
            stdout.write(blank_line_separator(tail, tty=False))
            stdout.write(format_footer(response, seconds))
    try:
        while True:
            if tty:  # pragma: no cover - real TTY integration
                assert tui is not None
                line = await tui.next_input()
                if line == QUIT_SENTINEL:
                    break
                if line.strip():
                    stdout.write(f"❯ {line}\n")
                line += "\n"
            else:
                stdout.write("bia> ")
                stdout.flush()
                line = await asyncio.to_thread(stdin.readline)
            if not line:
                break
            raw = line.strip()
            if not raw:
                continue
            if raw in {"/exit", "/quit"}:
                break
            if raw.startswith("/help"):
                parts = raw.split(maxsplit=1)
                try:
                    stdout.write(command_help(parts[1] if len(parts) == 2 else None))
                except KeyError:
                    stderr.write(f"Unknown command: {parts[1]}\n")
                continue
            if raw == "/model":
                if tui is not None:
                    await tui.pause()
                selection = await configure_model(
                    Settings.from_env(), ask=ask, ask_secret=ask_secret, stdout=stdout,
                    stderr=stderr, interactive=tty,
                )
                if selection is not None:
                    model_state["label"] = selection.model
                if tui is not None:
                    tui.resume()
                continue
            if raw in {"/loop", "/loop status", "/loop services", "/loop lag",
                       "/loop checkpoints"}:
                snapshot = components.engine.health()
                if raw == "/loop services":
                    stdout.writelines(
                        f"{name:<24} {state.value}\n"
                        for name, state in snapshot.services.items()
                    )
                elif raw in {"/loop lag", "/loop checkpoints"}:
                    operational = await components.service.operational_snapshot()
                    if raw == "/loop lag":
                        lag = cast(dict[str, int], operational["lag"])
                        stdout.write(
                            f"commands={lag['commands']} outbox={lag['outbox']}\n"
                        )
                    else:
                        checkpoints = cast(list[dict[str, object]], operational["checkpoints"])
                        if not checkpoints:
                            stdout.write("No schedule checkpoints.\n")
                        else:
                            stdout.writelines(
                                f"{item['schedule_id']} {item['occurrence_key']} {item['status']}\n"
                                for item in checkpoints
                            )
                else:
                    stdout.write(
                        f"LoopEngine {snapshot.system.value} · instance {snapshot.instance_id}\n"
                    )
                continue
            if raw == "/img":
                kind, payload = capture_clipboard()
                if kind == "image" and payload:
                    token = register_image(payload)
                    pending_images.append(payload)
                    stdout.write(f"✓ 已添加 {token}，下一条消息将自动附带。\n")
                elif kind == "text" and payload:
                    stdout.write("剪贴板里是文本，直接输入发送即可（/img 只附带图片）。\n")
                else:
                    stdout.write("剪贴板里没有图片（或 PowerShell 不可用）。\n")
                continue
            if raw == "/clear":
                if chat is not None:
                    chat.clear()
                stdout.write("✓ 已清空对话上下文。\n")
                continue
            if not raw.startswith("/"):
                await chat_turn(raw)
                continue
            if raw.startswith("/") and " " not in raw:
                matches = tuple(command for command in COMMANDS if command.startswith(raw))
                if matches and raw not in COMMANDS:
                    stdout.write("Matches: " + "  ".join(matches) + "\n")
                    continue
            try:
                arguments = slash_arguments(raw)
            except ValueError as error:
                stderr.write(f"Invalid command: {error}\n")
                continue
            if arguments is None:
                stderr.write("Unknown command. Type /help.\n")
                continue
            command_out, command_err = StringIO(), StringIO()
            code = await run(
                ("--database", str(database_path), *arguments),
                command_out, command_err, stdin,
            )
            stdout.write(command_out.getvalue())
            stderr.write(command_err.getvalue())
            if code != EXIT_OK and not command_err.getvalue():
                stderr.write(f"Command exited with status {code}.\n")
            if serving.done():
                await serving
    finally:
        components.engine.request_shutdown()
        await serving
        if tui is not None:
            tui.stop()
            await tui.wait_stopped()
        await components.database.close()
    real_stdout.write("BIA terminal stopped.\n")
    real_stdout.flush()
    return 0


def slash_arguments(raw: str) -> tuple[str, ...] | None:
    if not raw.startswith("/"):
        return None
    values = shlex.split(raw[1:])
    if not values:
        return None
    command, rest = values[0], values[1:]
    if command == "market":
        if rest and rest[0] == "summary":
            return ("market", *rest)
        symbols = rest[0] if rest and not rest[0].startswith("--") else "INDEX.TEST"
        tail = rest[1:] if rest and not rest[0].startswith("--") else rest
        return ("market", "summary", "--symbols", symbols, *tail)
    if command == "insights" and not rest:
        return ("insights", "latest")
    if command == "subscribe":
        return ("subscriptions", "add", rest[0] if rest else "local-user")
    if command == "deliveries":
        return ("subscriptions", "list", rest[0] if rest else "local-user")
    if command == "trace":
        return ("replay", *rest)
    if command == "loop":
        return ("loop", *(rest or ("status",)))
    if command in {"system", "brain", "events", "plans", "tasks", "catalog", "skills",
                   "workflows", "dna"}:
        return (command, *(rest or ()))
    if command == "subscriptions":
        return (command, *(rest or ("list",)))
    if command in {"commands", "insights", "health", "status", "diagnose", "metrics", "log", "model", "clear"}:
        return (command, *rest)
    return None
