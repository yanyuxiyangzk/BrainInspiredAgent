import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from active_agent_platform.foundation import (
    CapturingLogger,
    FakeClock,
    FakeUuidGenerator,
    RuntimeDependencies,
    Settings,
)
from active_agent_platform.runtime import SystemHealth
from active_agent_platform.storage import DEFAULT_MIGRATIONS
from apps.hello_research import HelloResearchPlugin
from apps.hello_research.__main__ import main as example_main
from domain_sdk import (
    CapabilityContract,
    CompositionRoot,
    LoopProfile,
    PluginCatalog,
    PluginContribution,
    PluginValidationError,
    SideEffect,
    SkillManifest,
    SkillRegistration,
    WorkflowRegistration,
)
from domain_sdk.contracts import JsonValue

NOW = datetime(2026, 8, 17, tzinfo=UTC)


def dependencies(*ids: int) -> RuntimeDependencies:
    values = ids or (1,)
    return RuntimeDependencies(
        Settings(environment="test"),
        FakeClock(NOW),
        FakeUuidGenerator(UUID(int=value) for value in values),
        CapturingLogger(),
    )


class StaticPlugin:
    def __init__(self, contribution: PluginContribution) -> None:
        self._contribution = contribution

    def contribute(self) -> PluginContribution:
        return self._contribution


class EchoSkill:
    async def invoke(self, input_data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return dict(input_data)


class EmptyService:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        pass

    async def serve(self) -> None:
        pass

    async def quiesce(self) -> None:
        pass

    async def checkpoint(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def basic_capability(name: str = "research.text.echo") -> CapabilityContract:
    return CapabilityContract(name, "1.0", {"type": "object"}, {"type": "object"})


def basic_skill(capability: str = "research.text.echo") -> SkillRegistration:
    return SkillRegistration(
        SkillManifest("echo-skill", "1.0.0", "sha256:echo", (capability,)), EchoSkill()
    )


def basic_workflow(capability: str = "research.text.echo") -> WorkflowRegistration:
    return WorkflowRegistration("echo_workflow", "1.0.0", {"nodes": []}, (capability,))


def test_hello_research_registers_all_portability_extensions() -> None:
    catalog = PluginCatalog.from_plugins([HelloResearchPlugin()])

    assert list(catalog.capabilities) == ["research.text.normalize"]
    assert set(catalog.skills) == {("text-upper", "1.0.0"), ("text-lower", "1.0.0")}
    assert list(catalog.workflows) == [("normalize_research_note", "1.0.0")]
    assert list(catalog.loop_profiles) == [("research-note-review", "1.0.0")]
    assert len(catalog.evaluators) == 1
    assert isinstance(catalog.capabilities, MappingProxyType)
    with pytest.raises(TypeError):
        catalog.capabilities["new"] = basic_capability()  # type: ignore[index]


@pytest.mark.asyncio
async def test_two_skills_are_interchangeable_for_same_capability() -> None:
    catalog = PluginCatalog.from_plugins([HelloResearchPlugin()])
    upper = catalog.skills[("text-upper", "1.0.0")].adapter
    lower = catalog.skills[("text-lower", "1.0.0")].adapter

    assert await upper.invoke({"text": "Mixed Case"}) == {
        "text": "MIXED CASE",
        "style": "upper",
    }
    assert await lower.invoke({"text": "Mixed Case"}) == {
        "text": "mixed case",
        "style": "lower",
    }
    evaluator = catalog.evaluators[0]
    assert await evaluator.evaluate({"text": "result"}) == {"accepted": True, "length": 6}
    assert await evaluator.evaluate({"text": " "}) == {"accepted": False, "length": 1}


@pytest.mark.asyncio
async def test_composed_application_migrates_database_and_stops_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "application.db"
    application = CompositionRoot(
        dependencies(), path, [HelloResearchPlugin()]
    ).build()
    application.request_shutdown()
    await application.run()

    assert application.health().system is SystemHealth.STOPPED
    with sqlite3.connect(path) as connection:
        migration_count = connection.execute("SELECT count(*) FROM schema_migration").fetchone()
    assert migration_count == (len(DEFAULT_MIGRATIONS),)


@pytest.mark.asyncio
async def test_example_module_is_directly_runnable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await example_main()
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "capabilities": 1,
        "evaluators": 1,
        "loop_profiles": 1,
        "skills": 2,
        "status": "STOPPED",
        "workflows": 1,
    }


def test_duplicate_plugin_and_registration_keys_are_rejected() -> None:
    contribution = PluginContribution("duplicate_plugin", capabilities=(basic_capability(),))
    with pytest.raises(PluginValidationError, match="plugin IDs"):
        PluginCatalog.from_plugins([StaticPlugin(contribution), StaticPlugin(contribution)])

    first = PluginContribution("first_plugin", capabilities=(basic_capability(),))
    second = PluginContribution("second_plugin", capabilities=(basic_capability(),))
    with pytest.raises(PluginValidationError, match="duplicate capability"):
        PluginCatalog.from_plugins([StaticPlugin(first), StaticPlugin(second)])

    duplicate_skills = PluginContribution(
        "skill_plugin",
        capabilities=(basic_capability(),),
        skills=(basic_skill(), basic_skill()),
    )
    with pytest.raises(PluginValidationError, match="duplicate skill"):
        PluginCatalog.from_plugins([StaticPlugin(duplicate_skills)])

    duplicate_workflows = PluginContribution(
        "workflow_plugin",
        capabilities=(basic_capability(),),
        workflows=(basic_workflow(), basic_workflow()),
    )
    with pytest.raises(PluginValidationError, match="duplicate workflow"):
        PluginCatalog.from_plugins([StaticPlugin(duplicate_workflows)])

    profile = LoopProfile("profile", "1.0.0", "echo_workflow", "manual")
    duplicate_profiles = PluginContribution(
        "profile_plugin",
        capabilities=(basic_capability(),),
        workflows=(basic_workflow(),),
        loop_profiles=(profile, profile),
    )
    with pytest.raises(PluginValidationError, match="duplicate loop profile"):
        PluginCatalog.from_plugins([StaticPlugin(duplicate_profiles)])


def test_unknown_capability_and_workflow_references_are_rejected() -> None:
    unknown_skill = PluginContribution("unknown_skill", skills=(basic_skill("missing.cap"),))
    with pytest.raises(PluginValidationError, match="unknown capabilities"):
        PluginCatalog.from_plugins([StaticPlugin(unknown_skill)])

    unknown_workflow = PluginContribution(
        "unknown_workflow",
        workflows=(basic_workflow("missing.cap"),),
    )
    with pytest.raises(PluginValidationError, match="unknown capabilities"):
        PluginCatalog.from_plugins([StaticPlugin(unknown_workflow)])

    unknown_profile = PluginContribution(
        "unknown_profile",
        loop_profiles=(LoopProfile("profile", "1.0.0", "missing", "manual"),),
    )
    with pytest.raises(PluginValidationError, match="unknown workflow"):
        PluginCatalog.from_plugins([StaticPlugin(unknown_profile)])


def test_duplicate_service_names_are_rejected_across_plugins() -> None:
    first = PluginContribution("service_one", services=(EmptyService("same"),))
    second = PluginContribution("service_two", services=(EmptyService("same"),))
    with pytest.raises(PluginValidationError, match="service names"):
        PluginCatalog.from_plugins([StaticPlugin(first), StaticPlugin(second)])


def test_sdk_contracts_validate_identifiers_versions_and_digests() -> None:
    assert basic_capability().side_effect is SideEffect.PURE
    with pytest.raises(ValueError, match="dotted"):
        basic_capability("invalid")
    with pytest.raises(ValueError, match="version"):
        CapabilityContract("research.text.echo", "0.1", {}, {})
    with pytest.raises(ValueError, match="skill_id"):
        SkillManifest("X", "1.0.0", "sha256:x", ("research.text.echo",))
    with pytest.raises(ValueError, match="sha256"):
        SkillManifest("echo-skill", "1.0.0", "bad", ("research.text.echo",))
    with pytest.raises(ValueError, match="non-empty and unique"):
        SkillManifest("echo-skill", "1.0.0", "sha256:x", ())
    with pytest.raises(ValueError, match="workflow_id"):
        WorkflowRegistration("X", "1.0.0", {}, ("research.text.echo",))
    with pytest.raises(ValueError, match="required_capabilities"):
        WorkflowRegistration("valid_workflow", "1.0.0", {}, ())
    with pytest.raises(ValueError, match="must not be empty"):
        LoopProfile("", "1.0.0", "workflow", "manual")
    with pytest.raises(ValueError, match="plugin_id"):
        PluginContribution("X")
