from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_agent_platform.storage import DEFAULT_MIGRATIONS, SQLiteDatabase
from domain_sdk import (
    CapabilityContract,
    CompatibilityError,
    PluginContribution,
    RuntimeBuilder,
    SideEffect,
    assert_schema_backward_compatible,
    public_api_manifest,
)
from domain_sdk.registry import PluginCatalog


def test_public_api_manifest_and_legacy_plugin_are_accepted() -> None:
    manifest = public_api_manifest()
    assert manifest.api_version == "1.0"
    assert {"RuntimeBuilder", "DomainPlugin", "DomainSkillBridge"} <= set(manifest.symbols)

    class LegacyPlugin:
        def contribute(self) -> PluginContribution:
            return PluginContribution(
                "legacy_research",
                capabilities=(CapabilityContract(
                    "legacy.research", "1.0", {"type": "object"}, {"type": "object"}, SideEffect.PURE,
                ),),
                sdk_api_version="1.0",
            )

    catalog = PluginCatalog.from_plugins([LegacyPlugin()])
    assert catalog.plugin_ids == ("legacy_research",)
    assert RuntimeBuilder(":memory:").with_plugin(LegacyPlugin()).build().catalog.plugin_ids == (
        "legacy_research",
    )


def test_future_plugin_major_is_rejected_before_runtime_start() -> None:
    class FuturePlugin:
        def contribute(self) -> PluginContribution:
            return PluginContribution("future_plugin", sdk_api_version="2.0")

    with pytest.raises(CompatibilityError, match="plugin SDK API"):
        PluginCatalog.from_plugins([FuturePlugin()])


def test_schema_compatibility_allows_optional_fields_and_rejects_breaking_changes() -> None:
    old = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    new = {"type": "object", "properties": {
        "text": {"type": "string"}, "trace": {"type": "string"},
    }, "required": ["text"]}
    assert_schema_backward_compatible(old, new)
    with pytest.raises(CompatibilityError, match="required"):
        assert_schema_backward_compatible(old, new | {"required": ["text", "trace"]})
    with pytest.raises(CompatibilityError, match="enum"):
        assert_schema_backward_compatible({"type": "string", "enum": ["A", "B"]},
                                          {"type": "string", "enum": ["A"]})
    with pytest.raises(CompatibilityError, match="type"):
        assert_schema_backward_compatible({"type": "string"}, {"type": "integer"})
    with pytest.raises(CompatibilityError, match="const"):
        assert_schema_backward_compatible({"type": "string", "const": "1.0"},
                                          {"type": "string", "const": "2.0"})
    closed = old | {"additionalProperties": False}
    with pytest.raises(CompatibilityError, match="removed property"):
        assert_schema_backward_compatible(closed, {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        })
    with pytest.raises(CompatibilityError, match="previously accepted"):
        assert_schema_backward_compatible(old, closed)


def test_all_versioned_schema_documents_are_self_compatible() -> None:
    root = Path(__file__).parents[1] / "schemas"
    files = sorted(root.rglob("*.schema.json"))
    assert len(files) >= 15
    for path in files:
        document = json.loads(path.read_text())
        assert_schema_backward_compatible(document, document)


@pytest.mark.asyncio
async def test_old_database_is_upgraded_forward_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    old = SQLiteDatabase(path, migrations=DEFAULT_MIGRATIONS[:5])
    await old.initialize()
    async with old.transaction() as tx:
        await tx.execute(
            "INSERT INTO plan(plan_id,plan_json,digest,status,created_at,expires_at,correlation_id) VALUES (?,?,?,?,?,?,?)",
            ("legacy-plan", "{}", "digest", "CANDIDATE", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "legacy-correlation"),
        )
    await old.close()

    upgraded = SQLiteDatabase(path)
    await upgraded.initialize()
    row = await upgraded.fetch_one("SELECT plan_id FROM plan WHERE plan_id='legacy-plan'")
    migrations = await upgraded.fetch_all("SELECT version FROM schema_migration ORDER BY version")
    assert row is not None and len(migrations) == len(DEFAULT_MIGRATIONS)
    await upgraded.close()
