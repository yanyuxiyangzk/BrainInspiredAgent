"""全屏 TUI：上方滚动转录区 + 底部固定输入框与状态行。

整块界面运行在备用屏幕缓冲里：输入框永远钉在底部，命令列表在输入框上方
展开（最多 10 行、↑↓ 滚动、回车应用），所有输出都追加进转录区并自动滚到
最新一行。退出时恢复原终端画面。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
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

from apps.quant_agent.commands import COMMAND_SPECS

TUI_STYLE = Style.from_dict({
    "arrow": "bold #38bdf8",
    "rule": "#5c6370",
    "status-left": "#7a828a",
    "status-model": "#e5c07b bold",
    "status-usage": "#7a828a",
    "thinking": "#7dd3fc",
    "menu-row": "#e2e8f0",
    "menu-selected": "bg:#38bdf8 #08111a bold",
    "transcript": "",
})

STATUS_HINTS = "? 直接输入与模型对话 · /img 或 Ctrl+V 贴图 · /help"
MENU_ROWS = 10
QUIT_SENTINEL = "\x00quit"


class TranscriptWriter:
    """stdout/stderr 代理：把写入内容追加进转录区。"""

    def __init__(self, tui: BiaTui) -> None:
        self._tui = tui

    def write(self, text: str) -> None:
        if text:
            self._tui.append(text)

    def flush(self) -> None:
        return None


def menu_viewport(total: int, index: int, offset: int, visible: int) -> int:
    """Scroll offset that keeps ``index`` inside a ``visible``-row window."""
    offset = max(offset, index - visible + 1)
    offset = min(offset, index)
    return max(0, min(offset, max(0, total - visible)))


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


class BiaTui:
    """全屏对话界面：转录区（输出）+ 底部输入框 + 状态行。"""

    def __init__(self, model_label: Callable[[], str]) -> None:
        self._model_label = model_label
        self.on_image: Callable[[str], str] | None = None
        self._menu_index = 0
        self._menu_offset = 0
        self._menu_hidden = False
        self.usage_text = ""
        self.thinking_text = ""
        self.paste_note = ""
        self._thinking_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.transcript = Buffer(multiline=True)
        self.input = Buffer(
            multiline=True,
            completer=SlashCompleter(),
            complete_while_typing=True,
            accept_handler=self._accept,
            on_text_changed=lambda buffer: self._reset_menu(),
        )
        self._app: Application[None] | None = None
        self._app_task: asyncio.Task[None] | None = None

    # —— 生命周期 ——

    def start(self) -> None:
        self._app = self._build_app()
        self._app_task = asyncio.create_task(self._app.run_async())

    def stop(self) -> None:
        if self._app is not None:
            self._app.exit()

    async def wait_stopped(self) -> None:
        if self._app_task is not None:
            try:
                await self._app_task
            except asyncio.CancelledError:
                pass
            self._app_task = None

    async def pause(self) -> None:
        """退出全屏（恢复原终端），用于 /model 等需要普通终端交互的流程。"""
        self.stop()
        await self.wait_stopped()

    def resume(self) -> None:
        self.start()

    # —— 输入 ——

    async def next_input(self) -> str:
        return await self._queue.get()

    def _accept(self, buffer: Buffer) -> bool:
        text = buffer.text
        buffer.reset()
        self._reset_menu()
        self._queue.put_nowait(text)
        return True

    def _reset_menu(self) -> None:
        self._menu_index = 0
        self._menu_hidden = False

    def current_completions(self) -> list[Completion]:
        state = self.input.complete_state
        return list(state.completions) if state and state.completions else []

    # —— 输出 ——

    def append(self, text: str) -> None:
        if not text:
            return
        self.transcript.insert_text(text)
        self.transcript.cursor_position = len(self.transcript.text)
        self._invalidate()

    def set_thinking(self, text: str | None) -> None:
        self.thinking_text = text or ""
        self._invalidate()

    def start_thinking(self) -> None:
        self.thinking_text = "✻ 思考中… 0s"
        if self._thinking_task is None:
            self._thinking_task = asyncio.create_task(self._thinking_spin())
        self._invalidate()

    def stop_thinking(self) -> None:
        if self._thinking_task is not None:
            self._thinking_task.cancel()
            self._thinking_task = None
        self.thinking_text = ""
        self._invalidate()

    def set_usage(self, text: str) -> None:
        self.usage_text = text
        self._invalidate()

    def _invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    async def _thinking_spin(self) -> None:  # pragma: no cover - 实时动画
        started = time.monotonic()
        position = 0
        while True:
            elapsed = time.monotonic() - started
            self.set_thinking(f"✻ 思考中… {elapsed:.0f}s")
            position += 1
            await asyncio.sleep(0.4)

    # —— 渲染 ——

    def _width(self) -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).columns - 1

    def _render_rules(self):  # type: ignore[no-untyped-def]
        return [("class:rule", "─" * max(8, self._width()))]

    def _render_status(self):  # type: ignore[no-untyped-def]
        from apps.quant_agent.chat import _display_width

        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        left = STATUS_HINTS
        right = f"◆ {self._model_label()}"
        if self.thinking_text:
            right += f" · {self.thinking_text}"
        elif self.usage_text:
            right += f" · {self.usage_text}"
        pad = max(2, width - _display_width(left) - _display_width(right) - 2)
        return [
            ("class:status-left", left),
            ("class:status-pad", " " * pad),
            ("class:status-model", right),
        ]

    def _render_menu(self):  # type: ignore[no-untyped-def]
        if self._menu_hidden:
            return []
        completions = self.current_completions()
        if not completions:
            return []
        shown = menu_viewport(
            len(completions), self._menu_index, self._menu_offset, MENU_ROWS,
        )
        self._menu_offset = shown
        visible = completions[shown: shown + MENU_ROWS]
        rows = []
        for position, item in enumerate(visible, start=shown):
            style = "class:menu-selected" if position == self._menu_index else "class:menu-row"
            summary = item.display_meta_text or ""
            rows.append((style, f"  {item.text}  {summary}\n"))
        return rows

    def _build_app(self) -> Application[None]:
        bindings = KeyBindings()

        @bindings.add("c-c")
        def _quit(event: object) -> None:
            self.stop()
            self._queue.put_nowait(QUIT_SENTINEL)

        @bindings.add("escape")
        def _clear(event: object) -> None:
            self.input.reset()

        layout = Layout(HSplit([
            Window(
                content=BufferControl(buffer=self.transcript),
                wrap_lines=True,
                height=Dimension(weight=1),
            ),
            Window(
                content=FormattedTextControl(self._render_menu),
                height=Dimension(min=0, max=MENU_ROWS),
            ),
            Window(FormattedTextControl(self._render_rules), height=1),
            VSplit([
                Window(
                    content=FormattedTextControl([("class:arrow", "❯ ")]),
                    width=2,
                ),
                Window(
                    content=BufferControl(buffer=self.input),
                    wrap_lines=True,
                    height=Dimension(min=1, max=6),
                ),
            ]),
            Window(FormattedTextControl(self._render_status), height=1),
            Window(FormattedTextControl(self._render_rules), height=1),
        ]))
        return Application(
            layout=layout, key_bindings=bindings, style=TUI_STYLE, full_screen=True,
        )

    async def run(self) -> None:
        self.start()
        await self.wait_stopped()
