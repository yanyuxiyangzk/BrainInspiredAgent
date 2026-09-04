"""Interactive slash-command terminal for the local quant agent."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (  # type: ignore[attr-defined]
    ConditionalContainer,
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
    MarkdownStreamFormatter,
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

# Ctrl+C returns this sentinel from the prompt, which PromptHandle.read()
# turns into a KeyboardInterrupt so the shell exits like it did before.
CTRL_C_SENTINEL = "\x00bia-quit"


class PromptHandle:  # pragma: no cover - real TTY integration
    """One long-lived prompt application for the whole interactive session.

    The accept key binding hands the typed text to the main loop via
    :meth:`accept` without exiting the application, so the input frame stays
    rendered while a reply streams into the terminal space below it (see
    :meth:`terminal`). Verified through pty black-box tests in
    ``tests/test_quant_shell.py``-style harnesses rather than unit tests.
    """

    def __init__(
        self, builder: Callable[[PromptHandle], tuple[Application[str], Buffer]],
    ) -> None:
        self._builder = builder
        self.application: Application[str] | None = None
        self.buffer: Buffer | None = None
        self.accept_event = asyncio.Event()
        self.accepted = ""
        self.run_task: asyncio.Task[str] | None = None

    def start(self) -> None:
        """Build and start the prompt application."""
        self.accept_event = asyncio.Event()
        self.accepted = ""
        self.application, self.buffer = self._builder(self)
        self.run_task = asyncio.create_task(self.application.run_async())

    def accept(self, text: str) -> None:
        self.accepted = text
        self.accept_event.set()

    def stop(self) -> None:
        """Exit the application; the frame is erased via ``erase_when_done``."""
        if (
            self.application is not None
            and self.run_task is not None
            and not self.run_task.done()
        ):
            self.application.exit()

    async def reap(self) -> None:
        """Await the application task, swallowing late render errors."""
        if self.run_task is None:
            return
        with contextlib.suppress(Exception):
            await self.run_task

    def invalidate(self) -> None:
        """Redraw the frame (e.g. after the status text changed)."""
        if self.application is not None:
            self.application.invalidate()

    def emitter(self, stream: TextIO) -> Callable[[str], Awaitable[None]]:
        """Build an async writer whose output scrolls in above the frame."""
        async def emit(text: str) -> None:
            if not text:
                return
            if self.application is not None and self.run_task and not self.run_task.done():
                await run_in_terminal(lambda: stream.write(text))
            else:
                stream.write(text)
        return emit

    async def _wait_accept(self) -> None:
        await self.accept_event.wait()

    async def _wait_app_ended(self) -> None:
        if self.run_task is not None:
            with contextlib.suppress(Exception):
                await self.run_task

    async def read(self) -> str:
        """Wait for the next accepted, non-empty input line."""
        while True:
            if self.run_task is not None and self.run_task.done():
                # The application ended on its own; treat it as EOF.
                with contextlib.suppress(Exception):
                    await self.run_task
                raise KeyboardInterrupt
            waiter = asyncio.ensure_future(self._wait_accept())
            run_waiter: asyncio.Task[None] | None = None
            if self.run_task is not None:
                run_waiter = asyncio.ensure_future(self._wait_app_ended())
            pending: list[asyncio.Task[None]] = [waiter]
            if run_waiter is not None:
                pending.append(run_waiter)
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if run_waiter is not None and run_waiter in done and not self.accept_event.is_set():
                # The application ended on its own; treat it as EOF.
                raise KeyboardInterrupt
            self.accept_event.clear()
            text = self.accepted
            self.accepted = ""
            if text == CTRL_C_SENTINEL:
                raise KeyboardInterrupt
            if text.strip():
                if self.buffer is not None:
                    self.buffer.reset()
                return text
            # Empty submit: the frame stays and we keep waiting.

BANNER = r"""
         ╭──╮   ╭──╮
     ╭───╯ ╭─╯ ╰─╮ ╰───╮
   ╭──╯   ╭─╯   ╰─╮   ╰──╮
 ╭╯      ╭─╮   ╭─╮      ╰╮
│ ──●    │ ╰─│─╯ │    ●── │            COGNITIVE AGENT
│ ──●   ╰──╮ │ ╭──╯   ●── │      类脑 Agent · Cognitive Runtime
│      ╭──╯  │  ╰──╮      │感知 · 记忆 · 推理 · 规划 · 执行 · 复盘
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
    "thinking-spin": "bold #7dd3fc",
    "thinking-tip": "#7a828a",
    "menu-row": "#e2e8f0",
    "menu-selected": "bg:#38bdf8 #08111a bold",
})

STATUS_HINTS = "? 直接输入与模型对话 · /img 或 Ctrl+V 贴图 · /help"

SPINNER = "✶✳✲✱✻✽"

THINKING_TIPS = (
    "Tip: 直接输入文字即可与模型对话",
    "Tip: /img 附带图片提问 · Ctrl+V 从剪贴板贴图",
    "Tip: Ctrl+J 插入换行 · Esc 清空当前输入",
    "Tip: /clear 清空上下文 · /model 更换模型",
)


def format_token_count(count: int) -> str:
    """Compact token count for the thinking indicator (1234 -> 1.2k)."""
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


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
    # 底部不再画线：首轮回显上方会画一条分隔线（turn_rule），
    # 欢迎语与对话历史之间由此隔开。
    return "\n".join([
        rule,
        f"  模型   {model_line}",
        "  上手   直接输入文字即可对话 · /img 贴图 · /help 全部命令",
    ]) + "\n"


def turn_rule() -> str:
    """Dim full-width rule drawn above the first echoed turn only.

    Separates the welcome panel from the conversation transcript; later turns
    follow each other without extra lines.
    """
    width = shutil.get_terminal_size(fallback=(80, 24)).columns - 1
    return f"\x1b[2m{'─' * max(8, width)}\x1b[0m\n"


def _shell_key_bindings(
    register_image: Callable[[str], str],
    paste_note: dict[str, str],
    accept: Callable[[str], None],
) -> KeyBindings:  # pragma: no cover - prompt-toolkit callbacks
    """Ctrl+J adds a newline; Ctrl+V/Alt+V paste; Ctrl+C hands back the sentinel."""
    bindings = KeyBindings()

    @bindings.add("c-c")
    def _quit(event: object) -> None:
        # Ctrl+C：把哨兵值交给主循环，由外层退出并回收应用。
        accept(CTRL_C_SENTINEL)

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
    thinking_state = {"active": False, "started": 0.0, "chars": 0}
    pending_images: list[str] = []
    image_registry: dict[str, str] = {}
    image_counter = {"n": 0}
    turn_counter = {"n": 0}

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
    handle: PromptHandle | None = None
    ticker: asyncio.Task[None] | None = None
    if tty:  # pragma: no cover - real TTY integration
        def render_rules() -> StyleAndTextTuples:
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            return [("class:rule", "─" * max(8, width - 1))]

        def render_thinking() -> StyleAndTextTuples:
            # 思考区块：耗时 + 流式 token 估算 + 轮换小贴士，画在框体顶线上方。
            elapsed = max(0.0, time.monotonic() - thinking_state["started"])
            whole = int(elapsed)
            minutes, seconds = divmod(whole, 60)
            clock = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            tokens = int(thinking_state["chars"]) // 2
            usage = f" · ↓{format_token_count(tokens)}" if tokens else ""
            tip = THINKING_TIPS[(whole // 10) % len(THINKING_TIPS)]
            return [
                ("class:thinking-spin", f"{SPINNER[whole % len(SPINNER)]} "),
                ("class:thinking", f"思考中… ({clock}{usage})\n"),
                ("class:thinking-tip", f"  ⎿ {tip}"),
            ]

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

        def build_prompt_app(handle: PromptHandle) -> tuple[Application[str], Buffer]:
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
            bindings = _shell_key_bindings(register_image, paste_note, handle.accept)

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
                handle.accept(buffer.document.text)

            @bindings.add("escape")
            def _cancel(event: object) -> None:
                handle.accept("")

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

            layout = Layout(HSplit([
                ConditionalContainer(
                    Window(
                        FormattedTextControl(render_thinking),
                        height=Dimension(min=0, max=2),
                        dont_extend_height=True,
                    ),
                    # 思考中区块只在等待回复时出现，位于框体顶线的上方。
                    filter=Condition(lambda: bool(thinking_state["active"])),
                ),
                Window(FormattedTextControl(render_rules), height=1),
                VSplit([
                    Window(FormattedTextControl([("class:arrow", "❯ ")]), width=2),
                    Window(
                        content=BufferControl(buffer=buffer),
                        wrap_lines=True,
                        height=Dimension(min=1, max=8),
                        # 不把剩余垂直空间撑给输入框：高度只随内容增长。
                        dont_extend_height=True,
                    ),
                ]),
                Window(FormattedTextControl(render_rules), height=1),
                # 状态行作为框体页脚，紧贴底部分割线的下方。
                Window(FormattedTextControl(render_status), height=1),
                ConditionalContainer(
                    Window(
                        content=FormattedTextControl(render_menu),
                        height=Dimension(min=0, max=12),
                        dont_extend_height=True,
                    ),
                    # 无补全时菜单整体移除，避免框体下方留出空行。
                    filter=Condition(
                        lambda: bool(current_completions()) and not menu_hidden[0]
                    ),
                ),
            ]))
            # erase_when_done 让 prompt_toolkit 退出时自行擦除输入框：它感知
            # 滚动，不会像手动"预留+擦除"那样在屏幕底部覆盖并吃掉历史回复。
            application: Application[str] = Application(
                layout=layout, key_bindings=bindings, style=SHELL_STYLE,
                full_screen=False, erase_when_done=True,
            )
            return application, buffer

        handle = PromptHandle(build_prompt_app)
    stdout.write(BANNER)
    stdout.write(welcome_panel(settings))
    stdout.flush()
    if handle is not None:  # pragma: no cover - real TTY integration
        handle.start()

        async def thinking_ticker() -> None:
            # 每秒重绘一次思考区块：耗时/token 估算每秒跳动，小贴士每 10s 轮换。
            while True:
                await asyncio.sleep(1.0)
                if thinking_state["active"]:
                    handle.invalidate()

        ticker = asyncio.create_task(thinking_ticker())
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

    async def chat_turn(
        text: str,
        emit: Callable[[str], Awaitable[None]],
        emit_err: Callable[[str], Awaitable[None]],
    ) -> None:
        nonlocal chat
        try:
            cleaned, images = extract_images(resolve_image_tokens(text))
            for path in pending_images:
                images = images + (image_data_url(path),)
        except ChatInputError as error:
            await emit_err(f"{error}\n")
            return
        if not cleaned:
            cleaned = "请看这张图片"
        missing = find_missing_images(text)
        if missing:
            await emit_err(
                "⚠ 未找到图片文件：" + "、".join(missing)
                + "（确认路径是否正确，或截图后用 /img 从剪贴板添加）\n"
            )
        if chat is None or chat.label != model_state["label"]:
            client = build_chat_client(Settings.from_env())
            if client is None:
                await emit_err("还没有配置模型：先输入 /model 选择模型，再直接输入文字对话。\n")
                return
            chat = ChatSession(client, label=model_state["label"])
        normalizer = WhitespaceNormalizer()
        markdown = MarkdownStreamFormatter() if tty else None

        def print_delta(delta: str) -> None:
            thinking_state["chars"] += len(delta)
            text = normalizer.feed(delta)
            if markdown is not None and text:
                text = markdown.feed(text)
            if text:
                asyncio.ensure_future(emit(text))

        if images:
            await emit(f"[已附带 {len(images)} 张图片]\n")
        started = time.monotonic()
        try:
            response = await chat.send(cleaned, on_delta=print_delta, images=images)
        except LlmError as error:
            await emit_err(describe_llm_error(error) + "\n")
            return
        pending_images.clear()
        tail = normalizer.flush()
        if markdown is not None:
            tail = markdown.feed(tail) + markdown.flush()
        if tail:
            if tty and not tail.endswith("\n"):
                tail += "\n"
            await emit(tail)
        seconds = time.monotonic() - started
        if not tty:
            await emit(blank_line_separator(tail, tty))
            await emit(format_footer(response, seconds))
        else:
            # 输入框（含状态行）仍活在上层：状态行随后显示本次用量。
            usage_state["text"] = usage_note(response, seconds)
            if handle is not None:
                handle.invalidate()

    async def write_echo(
        raw: str,
        emit: Callable[[str], Awaitable[None]],
        display: str | None = None,
    ) -> None:
        # 回显输入行；分隔线只画在首轮回显上方，隔开欢迎面板与对话历史。
        if not tty:
            return
        if not turn_counter["n"]:
            await emit(turn_rule())
        turn_counter["n"] += 1
        await emit(f"❯ {display or raw}\n")

    async def dispatch_line(
        raw: str,
        emit: Callable[[str], Awaitable[None]],
        emit_err: Callable[[str], Awaitable[None]],
    ) -> None:
        # /img <图片路径> [提问]：附带图片文件，可直接带提问发送。
        if raw.startswith("/img "):
            argument = raw[len("/img"):].strip()
            if argument.startswith('"'):
                path, _, rest = argument[1:].partition('"')
                question = rest.strip()
            else:
                path, _, question = argument.partition(" ")
                question = question.strip()
            try:
                image_data_url(path)  # 校验图片存在且大小合规
            except ChatInputError as error:
                await write_echo(raw, emit)
                await emit_err(f"{error}\n")
                return
            token = register_image(path)
            pending_images.append(path)
            await write_echo(raw, emit, display=f"[{token}] {question}".strip())
            if question:
                await chat_turn(question, emit, emit_err)
            else:
                await emit(f"✓ 已添加 [{token}]，下一条消息将自动附带。\n")
            return
        await write_echo(raw, emit)
        if raw.startswith("/help"):
            parts = raw.split(maxsplit=1)
            try:
                await emit(command_help(parts[1] if len(parts) == 2 else None))
            except KeyError:
                await emit_err(f"Unknown command: {parts[1]}\n")
            return
        if raw in {"/loop", "/loop status", "/loop services", "/loop lag",
                   "/loop checkpoints"}:
            snapshot = components.engine.health()
            if raw == "/loop services":
                await emit("".join(
                    f"{name:<24} {state.value}\n"
                    for name, state in snapshot.services.items()
                ))
            elif raw in {"/loop lag", "/loop checkpoints"}:
                operational = await components.service.operational_snapshot()
                if raw == "/loop lag":
                    lag = cast(dict[str, int], operational["lag"])
                    await emit(f"commands={lag['commands']} outbox={lag['outbox']}\n")
                else:
                    checkpoints = cast(list[dict[str, object]], operational["checkpoints"])
                    if not checkpoints:
                        await emit("No schedule checkpoints.\n")
                    else:
                        await emit("".join(
                            f"{item['schedule_id']} {item['occurrence_key']} {item['status']}\n"
                            for item in checkpoints
                        ))
            else:
                await emit(
                    f"LoopEngine {snapshot.system.value} · instance {snapshot.instance_id}\n"
                )
            return
        if raw == "/img":
            kind, payload = capture_clipboard()
            if kind == "image" and payload:
                token = register_image(payload)
                pending_images.append(payload)
                await emit(f"✓ 已添加 {token}，下一条消息将自动附带。\n")
            elif kind == "text" and payload:
                await emit("剪贴板里是文本，直接输入发送即可（/img 只附带图片）。\n")
            else:
                await emit("剪贴板里没有图片（或 PowerShell 不可用）。\n")
            return
        if raw.startswith("/img "):
            # /img <图片路径> [提问]：附带图片文件，可直接带提问发送。
            argument = raw[len("/img"):].strip()
            if argument.startswith('"'):
                path, _, rest = argument[1:].partition('"')
                question = rest.strip()
            else:
                path, _, question = argument.partition(" ")
                question = question.strip()
            try:
                image_data_url(path)  # 校验图片存在且大小合规
            except ChatInputError as error:
                await emit_err(f"{error}\n")
                return
            token = register_image(path)
            pending_images.append(path)
            await write_echo(raw, emit, display=(f"[{token}] {question}".strip()))
            if question:
                await chat_turn(question, emit, emit_err)
            else:
                await emit(f"✓ 已添加 [{token}]，下一条消息将自动附带。\n")
            return
        if raw == "/clear":
            if chat is not None:
                chat.clear()
            await emit("✓ 已清空对话上下文。\n")
            return
        if not raw.startswith("/"):
            await chat_turn(raw, emit, emit_err)
            return
        if raw.startswith("/") and " " not in raw:
            matches = tuple(command for command in COMMANDS if command.startswith(raw))
            if matches and raw not in COMMANDS:
                await emit("Matches: " + "  ".join(matches) + "\n")
                return
        try:
            arguments = slash_arguments(raw)
        except ValueError as error:
            await emit_err(f"Invalid command: {error}\n")
            return
        if arguments is None:
            await emit_err("Unknown command. Type /help.\n")
            return
        command_out, command_err = StringIO(), StringIO()
        code = await run(
            ("--database", str(database_path), *arguments),
            command_out, command_err, stdin,
        )
        await emit(command_out.getvalue())
        await emit_err(command_err.getvalue())
        if code != EXIT_OK and not command_err.getvalue():
            await emit_err(f"Command exited with status {code}.\n")
        if serving.done():
            await serving
    try:
        while True:
            if tty:  # pragma: no cover - real TTY integration
                assert handle is not None
                try:
                    raw = (await handle.read()).strip()
                except (EOFError, KeyboardInterrupt):
                    break
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
            if raw == "/model":
                # /model 需要嵌套交互输入：先退出主输入框，结束后重建。
                async def echo(text: str) -> None:
                    stdout.write(text)

                if handle is not None:
                    handle.stop()
                    await handle.reap()
                await write_echo(raw, echo)
                selection = await configure_model(
                    Settings.from_env(), ask=ask, ask_secret=ask_secret, stdout=stdout,
                    stderr=stderr, interactive=tty,
                )
                if selection is not None:
                    model_state["label"] = selection.model
                if handle is not None:
                    handle.start()
                continue
            if tty and handle is not None:  # pragma: no cover - real TTY integration
                # 思考区块画在框体顶线上方：耗时 + token 估算 + 小贴士。
                thinking_state.update(active=True, started=time.monotonic(), chars=0)
                handle.invalidate()
                try:
                    await dispatch_line(raw, handle.emitter(stdout), handle.emitter(stderr))
                finally:
                    thinking_state["active"] = False
                    handle.invalidate()
            else:
                async def emit(text: str) -> None:
                    stdout.write(text)

                async def emit_err(text: str) -> None:
                    stderr.write(text)

                await dispatch_line(raw, emit, emit_err)
    finally:
        if ticker is not None:
            ticker.cancel()
        if handle is not None:
            handle.stop()
            await handle.reap()
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
