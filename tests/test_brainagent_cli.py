"""brainagent CLI 与样例领域入口的覆盖测试（通用分支）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.brainagent_cli import run

PLUGIN_SPEC = "apps.hello_research.plugin:HelloResearchPlugin"


@pytest.mark.asyncio
async def test_start_status_and_health_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "brainagent.db"
    assert await run(("--database", str(database), "start")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY"

    assert await run((
        "--database", str(database), "--plugin", PLUGIN_SPEC, "status",
    )) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "hello_research" in payload["plugins"]
    assert payload["capabilities"] >= 1

    assert await run((
        "--database", str(database), "--plugin", PLUGIN_SPEC, "health",
    )) == 0
    health = json.loads(capsys.readouterr().out)
    assert health["readiness"] in {"HEALTHY", "DEGRADED"}


@pytest.mark.asyncio
async def test_metrics_and_invalid_plugin_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "brainagent.db"
    await run(("--database", str(database), "start"))
    capsys.readouterr()

    assert await run((
        "--database", str(database), "--plugin", PLUGIN_SPEC, "metrics",
    )) == 0
    assert "loop_lag_seconds" in capsys.readouterr().out

    with pytest.raises(ValueError, match="module:PluginClass"):
        await run((
            "--database", str(database), "--plugin", "not-a-valid-spec", "start",
        ))


@pytest.mark.asyncio
async def test_negative_run_seconds_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="negative"):
        await run((
            "--database", str(tmp_path / "ba.db"), "run", "--run-seconds", "-1",
        ))


def test_plugin_without_contribute_is_rejected() -> None:
    from apps.brainagent_cli import _load_plugins

    with pytest.raises(TypeError, match="contribute"):
        _load_plugins(["apps.brainagent_cli:main"])


def test_main_reports_plugin_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["brainagent", "--plugin", "bad-spec", "start"])
    from apps.brainagent_cli import main

    assert main() == 2
    assert "error" in json.loads(capsys.readouterr().out)
