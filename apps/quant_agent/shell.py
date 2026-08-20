"""Interactive slash-command terminal for the local quant agent."""

from __future__ import annotations

import asyncio
import shlex
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.layout.containers import Float
from prompt_toolkit.styles import Style

from apps.quant_agent.commands import COMMAND_SPECS, COMMANDS, command_help

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
"""

SHELL_STYLE = Style.from_dict({
    "prompt": "bold #38bdf8",
    "completion-menu.completion": "bg:ansidefault #e2e8f0",
    "completion-menu.completion.current": "bg:ansidefault #38bdf8 bold underline",
    "completion-menu.meta.completion": "bg:ansidefault #7dd3fc",
    "completion-menu.meta.completion.current": "bg:ansidefault #38bdf8 bold",
    "scrollbar.background": "bg:ansidefault",
    "scrollbar.button": "bg:ansidefault #38bdf8",
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
    live_session: PromptSession[str] | None = None
    if stdin.isatty() and stdout.isatty():
        live_session = PromptSession(
            completer=SlashCompleter(), complete_while_typing=True,
            complete_in_thread=False, style=SHELL_STYLE,
            include_default_pygments_style=False,
        )
        _align_completion_menu(live_session)
    stdout.write(BANNER)
    stdout.write(f"BIA terminal ready · {database_path.resolve()}\n")
    stdout.write("Type / to browse commands, use arrows to select, /exit to stop.\n")
    stdout.flush()
    try:
        while True:
            if live_session is not None:
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
    if command in {"commands", "insights", "health", "status", "diagnose", "metrics", "log"}:
        return (command, *rest)
    return None
