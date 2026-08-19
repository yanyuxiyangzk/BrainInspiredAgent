"""Public quant domain catalog for generic BrainAgent discovery."""

from collections.abc import Mapping

from domain_sdk import (
    CapabilityContract,
    DomainPlugin,
    LoopProfile,
    PluginContribution,
    SideEffect,
    SkillManifest,
    SkillRegistration,
    WorkflowRegistration,
)
from domain_sdk.contracts import JsonValue


class _CatalogOnlyAdapter:
    async def invoke(self, input_data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        raise RuntimeError("quant skills execute through the governed platform runtime")


class QuantDomainPlugin(DomainPlugin):
    """Expose quant capabilities without leaking them into Kernel or Platform."""

    def contribute(self) -> PluginContribution:
        specs = (
            ("market.snapshot.read", SideEffect.PURE, "fake-market-read"),
            ("content.summary.generate", SideEffect.PURE, "fake-summary"),
            ("notification.local.send", SideEffect.IDEMPOTENT, "local-notification"),
        )
        capabilities = tuple(
            CapabilityContract(name, "1.0", {"type": "object"}, {"type": "object"}, effect)
            for name, effect, _ in specs
        )
        skills = tuple(
            SkillRegistration(
                SkillManifest(skill, "1.0.0", f"sha256:quant-{skill}-v1", (name,)),
                _CatalogOnlyAdapter(),
            )
            for name, _, skill in specs
        )
        workflows = (
            WorkflowRegistration(
                "market_summary", "1.0.0",
                {"workflow_id": "market_summary", "version": "1.0.0"},
                tuple(item[0] for item in specs),
            ),
            WorkflowRegistration(
                "daily_review", "1.0.0",
                {"workflow_id": "daily_review", "version": "1.0.0"},
                ("content.summary.generate",),
            ),
        )
        return PluginContribution(
            plugin_id="quant_agent", capabilities=capabilities, skills=skills,
            workflows=workflows,
            loop_profiles=(
                LoopProfile("market-summary-command", "1.0.0", "market_summary", "command"),
                LoopProfile("daily-review-schedule", "1.0.0", "daily_review", "schedule"),
            ),
        )
