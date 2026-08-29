"""Interactive slash-command terminal for the local quant agent."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import time
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Float
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.styles import Style

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import LlmError
from apps.quant_agent.chat import (
    ChatSession,
    build_chat_client,
    describe_llm_error,
    format_reply,
)
from apps.quant_agent.commands import COMMAND_SPECS, COMMANDS, command_help
from apps.quant_agent.model_picker import OLLAMA_PROVIDER, configure_model

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
    "prompt": "bold #38bdf8",
    "completion-menu.completion": "bg:ansidefault #e2e8f0",
    "completion-menu.completion.current": "bg:ansidefault #38bdf8 bold underline",
    "completion-menu.meta.completion": "bg:ansidefault #7dd3fc",
    "completion-menu.meta.completion.current": "bg:ansidefault #38bdf8 bold",
    "scrollbar.background": "bg:ansidefault",
    "scrollbar.button": "bg:ansidefault #38bdf8",
    "bottom-toolbar": "noreverse bg:ansidefault",
    "toolbar-model": "#e5c07b bold",
    "toolbar-sep": "#7a828a",
    "toolbar-cwd": "#98c379",
})


class SlashCompleter(Completer):
    """Show the slash-command menu continuously while the user types."""

    def get_completions(self, document: Document, complete_event: object):  # type: ignore[no-untyped-def]
        del complete_event
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for spec in COMMAND_SPECS:
            if spec.name.startswith(text):
                yield Completion(
                    spec.name, start_position=-len(text), display=spec.name,
                    display_meta=spec.summary,
                )


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
    rule = "        " + "─" * 60
    return "\n".join([
        rule,
        f"          模型   {model_line}",
        "          提示   直接输入与模型对话 · /help 命令总览 · /model 切换模型 · /clear 清空 · Ctrl+J 换行",
        rule,
    ]) + "\n"


def _align_completion_menu(session: PromptSession[str]) -> None:
    """Anchor menu text under the slash instead of the moving cursor."""
    floats = _find_floats(session.app.layout.container)
    for item in floats[:2]:
        item.xcursor = False
        item.left = 4


def _find_floats(container: object) -> tuple[Float, ...]:
    floats = getattr(container, "floats", ())
    if floats:
        return cast(tuple[Float, ...], tuple(floats))
    nested = tuple(getattr(container, "children", ()))
    nested += tuple(
        value for name in ("content", "body", "alternative_content")
        if (value := getattr(container, name, None)) is not None
    )
    for child in nested:
        found = _find_floats(child)
        if found:
            return found
    return ()


def _shell_key_bindings() -> KeyBindings:  # pragma: no cover - prompt-toolkit callbacks
    """Keep Enter as submit while allowing Ctrl+J to add a new line."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()  # type: ignore[attr-defined]

    @bindings.add("c-j")
    def _insert_newline(event: object) -> None:
        # ``prompt_toolkit``'s event exposes the current buffer; keeping this
        # binding local avoids changing the global editing behaviour.
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    return bindings


def _set_input_height(session: PromptSession[str], minimum: int = 3) -> None:  # pragma: no cover
    """Give the multiline editor a comfortable three-line starting height."""
    for window in session.app.layout.find_all_windows():
        if (
            isinstance(window.content, BufferControl)
            and window.content.buffer is session.default_buffer
        ):
            # A plain integer is intentional here: prompt_toolkit otherwise
            # replaces the default dynamic height during the first render.
            window.height = minimum


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
    live_session: PromptSession[str] | None = None
    if stdin.isatty() and stdout.isatty():  # pragma: no cover - real TTY integration
        def toolbar() -> StyleAndTextTuples:
            cwd = os.getcwd()
            home = str(Path.home())
            if cwd.startswith(home):
                cwd = "~" + cwd[len(home):]
            fragments: StyleAndTextTuples = [
                ("class:toolbar-model", model_state["label"]),
                ("class:toolbar-sep", "  ·  "),
                ("class:toolbar-cwd", cwd),
            ]
            return fragments

        live_session = PromptSession(
            completer=SlashCompleter(), complete_while_typing=True,
            complete_in_thread=False, style=SHELL_STYLE,
            include_default_pygments_style=False,
            multiline=True,
            key_bindings=_shell_key_bindings(),
            bottom_toolbar=toolbar,
        )
        _align_completion_menu(live_session)
    stdout.write(BANNER)
    stdout.write(welcome_panel(settings))
    stdout.flush()
    async def ask(prompt: str) -> str:
        if live_session is not None:
            return (await live_session.prompt_async(prompt)).strip()
        stdout.write(prompt); stdout.flush()
        return (await asyncio.to_thread(stdin.readline)).strip()
    async def ask_secret(prompt: str) -> str:
        if live_session is not None:
            return (await live_session.prompt_async(prompt, is_password=True)).strip()
        stdout.write(prompt); stdout.flush()
        return (await asyncio.to_thread(stdin.readline)).strip()
    chat: ChatSession | None = None

    async def chat_turn(text: str) -> None:
        nonlocal chat
        if chat is None or chat.label != model_state["label"]:
            client = build_chat_client(Settings.from_env())
            if client is None:
                stderr.write("还没有配置模型：先输入 /model 选择模型，再直接输入文字对话。\n")
                return
            chat = ChatSession(client, label=model_state["label"])
        started = time.monotonic()
        try:
            response = await chat.send(text)
        except LlmError as error:
            stderr.write(describe_llm_error(error) + "\n")
            return
        tty = live_session is not None
        width = shutil.get_terminal_size(fallback=(80, 24)).columns if tty else 0
        stdout.write(format_reply(response, time.monotonic() - started, width, color=tty))
    try:
        while True:
            if live_session is not None:  # pragma: no cover - real TTY integration
                try:
                    line = await live_session.prompt_async([("class:prompt", "bia> ")])
                except (EOFError, KeyboardInterrupt):
                    break
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
                selection = await configure_model(
                    Settings.from_env(), ask=ask, ask_secret=ask_secret, stdout=stdout,
                    stderr=stderr, interactive=live_session is not None,
                )
                if selection is not None:
                    model_state["label"] = selection.model
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
        await components.database.close()
    stdout.write("BIA terminal stopped.\n")
    stdout.flush()
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
