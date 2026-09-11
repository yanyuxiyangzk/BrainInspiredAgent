"""Branch-completion tests for small application entry surfaces (CLI/facade/clipboard)."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from active_agent_platform.storage import SQLiteDatabase
from apps.brainagent_cli import _load_plugins, main, parser, run
from apps.quant_agent.clipboard import _to_wsl_path, capture_clipboard
from apps.quant_agent.execution_facade import QuantExecutionFacade


@pytest.mark.asyncio
async def test_plugin_loader_rejects_malformed_specs() -> None:
    with pytest.raises(ValueError, match="module:PluginClass"):
        _load_plugins(["just-a-module"])
    with pytest.raises(TypeError, match="does not implement"):
        _load_plugins(["json:JSONDecoder"])


class Plug:
    def contribute(self) -> str:
        return "ok"


def test_plugin_loader_accepts_class_specs() -> None:
    loaded = _load_plugins([f"{__name__}:Plug"])
    assert len(loaded) == 1 and hasattr(loaded[0], "contribute")


def test_main_reports_plugin_errors_as_machine_readable_exit(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "argv", ["bia", "--database", str(tmp_path / "x.db"), "--plugin", "bad", "run"])
    assert main() == 2


@pytest.mark.asyncio
async def test_run_rejects_negative_run_seconds(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="run-seconds"):
        await run(["--database", str(tmp_path / "x.db"), "run", "--run-seconds", "-1"])


def test_run_seconds_argument_parses_as_float() -> None:
    args = parser().parse_args(["run", "--run-seconds", "0.5"])
    assert args.run_seconds == 0.5


@pytest.mark.asyncio
async def test_execution_facade_requires_evaluator_and_guarded_dna_context(tmp_path: Any) -> None:
    facade = QuantExecutionFacade(motor=SimpleNamespace(execute=None, cancel=None))
    with pytest.raises(RuntimeError, match="evaluator"):
        await facade.evaluate(None)  # type: ignore[arg-type]

    without_database = QuantExecutionFacade(motor=SimpleNamespace(execute=None, cancel=None))
    await without_database.record_dna_context({})  # 无数据库：直接返回

    database = SQLiteDatabase(tmp_path / "facade.db")
    await database.initialize()
    guarded = QuantExecutionFacade(motor=SimpleNamespace(execute=None, cancel=None), database=database)
    await guarded.record_dna_context({"context_digest": "sha256:x"})  # 必填字段不全：不落库
    rows = await database.fetch_all("SELECT * FROM dna_execution_context")
    assert rows == []
    await database.close()


def test_capture_clipboard_handles_text_empty_and_unknown_output() -> None:
    def fake_run(output: str) -> Any:
        def run(*args: object, **kwargs: object) -> Any:
            return SimpleNamespace(stdout=output)

        return run

    kind, payload = capture_clipboard(fake_run("TXT:hello"))
    assert (kind, payload) == ("text", "hello")
    assert capture_clipboard(fake_run("TXT:")) == ("empty", None)
    assert capture_clipboard(fake_run("WEIRD:x")) == ("empty", None)


def test_wsl_path_passthrough_keeps_non_drive_paths() -> None:
    def empty_wslpath(*args: object, **kwargs: object) -> Any:
        return SimpleNamespace(stdout="")

    assert _to_wsl_path("relative/path.png", empty_wslpath) == "relative/path.png"
    assert _to_wsl_path("C:\\tmp\\x.png", empty_wslpath) == "/mnt/c/tmp/x.png"


def test_quant_main_module_exposes_cli_entry() -> None:
    import apps.quant_agent.__main__ as entry

    assert callable(entry.main)
