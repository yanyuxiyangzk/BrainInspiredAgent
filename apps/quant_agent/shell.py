"""Interactive slash-command terminal for the local quant agent."""

from __future__ import annotations

import asyncio
import functools
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (  # type: ignore[attr-defined]
    Dimension,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import LlmError
from apps.quant_agent.chat import (
    ChatInputError,
    ChatSession,
    WhitespaceNormalizer,
    _display_width,
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
from apps.quant_agent.commands import COMMAND_SPECS, COMMANDS, command_help
from apps.quant_agent.model_picker import OLLAMA_PROVIDER, configure_model

HELP = command_help()

# Rows pre-reserved before each prompt so the framed input always fits on
# screen: rule + pad + input (up to 8 lines) + pad + rule + status + slack.
PROMPT_RESERVED_ROWS = 18

# Ctrl+C returns this sentinel from the prompt, which read_line turns into
# a KeyboardInterrupt so the shell exits like it did before the redesign.
CTRL_C_SENTINEL = "\x00bia-quit"

BANNER = r"""
         ╭──╮   ╭──╮
     ╭───╯ ╭─╯ ╰─╮ ╰───╮
   ╭──╯   ╭─╯   ╰─╮   ╰──╮
 ╭╯      ╭─╮   ╭─╮      ╰╮
│ ──●    │ ╰─│─╯ │    ●── │      COGNITIVE AGENT
│ ──●   ╰──╮ │ ╭──╯   ●── │        类脑 Agent · Cognitive Runtime
│      ╭──╯  │  ╰──╮      │          感知 · 记忆 · 推理 · 规划 · 执行 · 复盘
│ ──●   ╭──╮ ┼ ╭──╮   ●── │
│     ╭─╯ ╰──┤──╯ ╰─╮     │
 ╰──╮   ╭──╯ │ ╰──╮   ╭──╯
   ╰───╮     │     ╭───╯
        ╰────┴────╯
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

STATUS_HINTS = "? 直接输入与模型对话 · /img 或 Ctrl+V 贴图 · /help"


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


def menu_viewport(total: int, index: int, offset: int, visible: int) -> int:
    """Scroll offset that keeps ``index`` inside a ``visible``-row window."""
    offset = max(offset, index - visible + 1)
    offset = min(offset, index)
    return max(0, min(offset, max(0, total - visible)))


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
        model_line = f"{settings.model_name} ✓ 已就绪 · /model 可更换"
    elif settings.model_name or settings.model_url:
        model_line = f"{settings.model_name or settings.model_url} · 缺少 API Key · /model 补充"
    else:
        model_line = "未配置 · /model 选择模型"
    width = shutil.get_terminal_size(fallback=(80, 24)).columns - 1
    rule = "─" * max(8, width)
    return "\n".join([
        rule,
        f"  模型   {model_line}",
        "  上手   直接输入文字即可对话 · /img 贴图 · /help 全部命令",
        f"  构建   {_build_tag()}",
        rule,
    ]) + "\n"


def _shell_key_bindings(
    register_image: Callable[[str], str],
    paste_note: dict[str, str],
) -> KeyBindings:  # pragma: no cover - prompt-toolkit callbacks
    """Ctrl+J adds a newline; Ctrl+V/Alt+V paste; Ctrl+C quits bia."""
    bindings = KeyBindings()

    @bindings.add("c-c")
    def _quit(event: object) -> None:
        # Ctrl+C 退出 bia：返回哨兵值，read_line 检测后抛出 KeyboardInterrupt。
        event.app.exit(CTRL_C_SENTINEL)  # type: ignore[attr-defined]

    @bindings.add("c-j")
    def _insert_newline(event: object) -> None:
        # ``prompt_toolkit``'s event exposes the current buffer; keeping this
        # binding local avoids changing the global editing behaviour.
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    @bindings.add("c-v")
    @bindings.add("escape", "v")
    def _paste_clipboard(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        app = event.app  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()

        def worker() -> None:
            kind, payload = capture_clipboard()

            def apply() -> None:
                paste_note["text"] = ""
                if kind == "image" and payload:
                    buffer.insert_text(f"[{register_image(payload)}]")
                elif kind == "text" and payload:
                    buffer.insert_text(payload)
                app.invalidate()

            loop.call_soon_threadsafe(apply)

        paste_note["text"] = " 读取剪贴板中…"
        app.invalidate()
        loop.run_in_executor(None, worker)

    return bindings


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
    usage_state: dict[str, str] = {"text": ""}
    paste_note: dict[str, str] = {"text": ""}
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
    sub_session: PromptSession[str] | None = None
    if tty:  # pragma: no cover - real TTY integration
        def render_rules() -> StyleAndTextTuples:
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            return [("class:rule", "─" * max(8, width - 1))]

        def render_status() -> StyleAndTextTuples:
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            left = STATUS_HINTS
            right = f"◆ {model_state['label']}"
            if usage_state["text"]:
                right += f" · {usage_state['text']}"
            if paste_note["text"]:
                right = f"{paste_note['text']}  {right}"
            pad = max(2, width - _display_width(left) - _display_width(right) - 2)
            return [
                ("class:status-left", left),
                ("class:status-pad", " " * pad),
                ("class:status-model", right),
            ]

        sub_session = PromptSession()

        def build_prompt_app() -> tuple[Application[str], Buffer]:
            menu_index = [0]
            menu_hidden = [False]

            def current_completions() -> list[Completion]:
                state = buffer.complete_state
                return list(state.completions) if state and state.completions else []

            def on_text_changed(buffer: Buffer) -> None:
                menu_index[0] = 0
                menu_hidden[0] = False

            buffer = Buffer(
                multiline=True, completer=SlashCompleter(), complete_while_typing=True,
                on_text_changed=on_text_changed,
            )
            bindings = _shell_key_bindings(register_image, paste_note)

            @bindings.add("enter")
            def _accept(event: object) -> None:
                completions = current_completions()
                index = menu_index[0]
                if completions and index < len(completions):
                    chosen = completions[index].text
                    if chosen != buffer.text:
                        buffer.text = chosen
                        buffer.cursor_position = len(chosen)
                        event.app.invalidate()  # type: ignore[attr-defined]
                        return
                event.app.exit(event.current_buffer.document.text)  # type: ignore[attr-defined]

            @bindings.add("escape")
            def _cancel(event: object) -> None:
                event.app.exit("")  # type: ignore[attr-defined]

            @bindings.add("up")
            def _menu_up(event: object) -> None:
                if current_completions() and not menu_hidden[0]:
                    menu_index[0] = max(0, menu_index[0] - 1)
                    event.app.invalidate()  # type: ignore[attr-defined]
                else:
                    event.current_buffer.cursor_up()  # type: ignore[attr-defined]

            @bindings.add("down")
            def _menu_down(event: object) -> None:
                completions = current_completions()
                if completions and not menu_hidden[0]:
                    menu_index[0] = min(len(completions) - 1, menu_index[0] + 1)
                    event.app.invalidate()  # type: ignore[attr-defined]
                else:
                    event.current_buffer.cursor_down()  # type: ignore[attr-defined]

            menu_offset = [0]

            def visible_menu_rows(total: int) -> int:
                rows = shutil.get_terminal_size(fallback=(80, 24)).lines
                return max(3, min(total, 12, rows - 8))

            def render_menu() -> StyleAndTextTuples:
                if menu_hidden[0]:
                    return []
                completions = current_completions()
                if not completions:
                    return []
                shown = visible_menu_rows(len(completions))
                menu_offset[0] = menu_viewport(
                    len(completions), menu_index[0], menu_offset[0], shown,
                )
                width = max(len(item.text) for item in completions)
                rows: StyleAndTextTuples = []
                for position, item in enumerate(
                    completions[menu_offset[0]: menu_offset[0] + shown],
                    start=menu_offset[0],
                ):
                    style = "class:menu-selected" if position == menu_index[0] else "class:menu-row"
                    summary = item.display_meta_text or ""
                    rows.append((style, f"  {item.text.ljust(width)}  {summary}\n"))
                return rows

            def render_filler() -> StyleAndTextTuples:
                # 尾部填充行吸收预留区的剩余行数：框体（横线+输入+状态行）
                # 保持紧凑，总高恒等于预留的 18 行，状态行不会被顶远。
                input_rows = min(max(len(buffer.document.lines), 1), 8)
                menu_rows = 0 if menu_hidden[0] else min(len(current_completions()), 12)
                slack = max(0, PROMPT_RESERVED_ROWS - 5 - input_rows - menu_rows)
                if slack <= 0:
                    return []
                return [("class:filler", "\n" * (slack - 1))]

            layout = Layout(HSplit([
                Window(FormattedTextControl(render_rules), height=1),
                VSplit([
                    Window(FormattedTextControl([("class:arrow", "❯ ")]), width=2),
                    Window(
                        content=BufferControl(buffer=buffer),
                        wrap_lines=True,
                        height=Dimension(min=1, max=8),
                    ),
                ]),
                Window(FormattedTextControl(render_status), height=1),
                Window(FormattedTextControl(render_rules), height=1),
                Window(
                    content=FormattedTextControl(render_menu),
                    height=Dimension(min=0, max=12),
                ),
                Window(
                    content=FormattedTextControl(render_filler),
                    height=Dimension(min=0, max=PROMPT_RESERVED_ROWS),
                ),
            ]))
            application: Application[str] = Application(
                layout=layout, key_bindings=bindings, style=SHELL_STYLE, full_screen=False,
            )
            return application, buffer

        async def read_line() -> str:
            # 预留 18 行并回退到预留区顶部：框体 + 菜单 + 尾部填充总高恒为 18。
            # 提交后擦除整块；思考提示行画在回显行上方，回复首字到达时清除。
            stdout.write("\n" * PROMPT_RESERVED_ROWS + "\x1b[1A" * PROMPT_RESERVED_ROWS)
            application, _ = build_prompt_app()
            text = await application.run_async()
            stdout.write("\x1b[2K" + "\x1b[1A\x1b[2K" * (PROMPT_RESERVED_ROWS - 1))
            if text == CTRL_C_SENTINEL:
                raise KeyboardInterrupt
            if text.strip():
                if tty:
                    stdout.write("  ✻ 思考中…\n")
                stdout.write(f"❯ {text}\n")
            return text
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

        def clear_thinking_above(echo_lines: int) -> str:
            """清掉回显行上方的"✻ 思考中…"行，光标回到回复起始行。"""
            if not tty:
                return ""
            up = echo_lines + 2
            return "\x1b[1A" * up + "\r\x1b[2K" + "\x1b[1B" * up

        def print_delta(delta: str) -> None:
            text = normalizer.feed(delta)
            if not emitted["chars"] and not text:
                return
            if not emitted["chars"]:
                stdout.write(clear_thinking_above(max(1, len(cleaned.splitlines()))))
                if images:
                    stdout.write(f"[已附带 {len(images)} 张图片]\n")
            emitted["chars"] += len(delta)
            stdout.write(text)
            stdout.flush()

        if tty:
            stdout.write(f"{STATUS_HINTS}    ◆ {model_state['label']}\n")
        started = time.monotonic()
        try:
            response = await chat.send(cleaned, on_delta=print_delta, images=images)
        except LlmError as error:
            stderr.write(clear_thinking_above(max(1, len(cleaned.splitlines()))))
            stderr.write(describe_llm_error(error) + "\n")
            return
        pending_images.clear()
        tail = normalizer.flush()
        if tail:
            stdout.write(tail)
        seconds = time.monotonic() - started
        stdout.write(blank_line_separator(tail, tty))
        if tty:
            usage_state["text"] = usage_note(response, seconds)
        else:
            stdout.write(format_footer(response, seconds))
    try:
        while True:
            if tty:  # pragma: no cover - real TTY integration
                try:
                    line = await read_line()
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
                    stderr=stderr, interactive=tty,
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
