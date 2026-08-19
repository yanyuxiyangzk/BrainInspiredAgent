from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from apps.brainagent_cli import run
from domain_sdk import RuntimeBuilder


def test_runtime_builder_is_domain_neutral() -> None:
    application = RuntimeBuilder(":memory:").build()
    assert application.catalog.plugin_ids == ()
    assert application.catalog.capabilities == {}


@pytest.mark.asyncio
async def test_generic_cli_loads_plugin_without_quant_dependency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = await run([
        "--plugin", "apps.hello_research:HelloResearchPlugin", "status",
    ])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plugins"] == ["hello_research"]
    assert payload["capabilities"] == 1


@pytest.mark.asyncio
async def test_generic_cli_lifecycle_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = str(tmp_path / "runtime.db")
    assert await run(["--database", database, "start"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY"
    assert await run(["--database", database, "health"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    assert await run(["--database", database, "diagnose"]) == 0
    assert "recent_errors" in json.loads(capsys.readouterr().out)
    assert await run(["--database", database, "run", "--run-seconds", "0"]) == 0


def test_runtime_builder_configuration() -> None:
    from active_agent_platform.foundation import (
        CapturingLogger,
        FakeClock,
        FakeUuidGenerator,
        RuntimeDependencies,
        Settings,
    )
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    dependencies = RuntimeDependencies(
        settings=Settings(environment="test"),
        clock=clock,
        uuid=FakeUuidGenerator([UUID(int=1)]),
        logger=CapturingLogger(),
    )
    from apps.hello_research import HelloResearchPlugin

    builder = RuntimeBuilder(":memory:").with_plugin(HelloResearchPlugin())
    builder.with_plugins(())
    assert builder.with_dependencies(dependencies).build().catalog.plugin_ids == ("hello_research",)


@pytest.mark.asyncio
async def test_external_research_project_uses_public_sdk_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    consumer_root = Path(__file__).parents[1] / "examples" / "research_agent"
    monkeypatch.syspath_prepend(str(consumer_root))
    result = await run([
        "--plugin", "research_agent:ResearchAgentPlugin", "status",
    ])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "plugins": ["research_agent"],
        "capabilities": 1,
        "skills": 1,
        "workflows": 1,
    }


def test_split_distribution_manifests_exclude_applications() -> None:
    root = Path(__file__).parents[1]
    expected = {
        "kernel": 'include = ["brain_kernel*"]',
        "platform": 'include = ["active_agent_platform*"]',
        "domain-sdk": 'include = ["domain_sdk*"]',
    }
    for distribution, include in expected.items():
        manifest = (root / "distributions" / distribution / "pyproject.toml").read_text()
        assert include in manifest
        assert "quant_agent" not in manifest
