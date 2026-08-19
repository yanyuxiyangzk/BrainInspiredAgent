"""Validated immutable snapshot assembled from domain plugins."""

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType

from brain_kernel.lifecycle import ManagedService
from domain_sdk.compatibility import SUPPORTED_PLUGIN_API_MAJOR, require_version
from domain_sdk.contracts import (
    CapabilityContract,
    DomainPlugin,
    LoopProfile,
    OutcomeEvaluator,
    SkillRegistration,
    WorkflowRegistration,
)


class PluginValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PluginCatalog:
    plugin_ids: tuple[str, ...]
    capabilities: MappingProxyType[str, CapabilityContract]
    skills: MappingProxyType[tuple[str, str], SkillRegistration]
    workflows: MappingProxyType[tuple[str, str], WorkflowRegistration]
    loop_profiles: MappingProxyType[tuple[str, str], LoopProfile]
    evaluators: tuple[OutcomeEvaluator, ...]
    services: tuple[ManagedService, ...]

    @classmethod
    def from_plugins(cls, plugins: Iterable[DomainPlugin]) -> "PluginCatalog":
        contributions = tuple(plugin.contribute() for plugin in plugins)
        plugin_ids = [contribution.plugin_id for contribution in contributions]
        if len(plugin_ids) != len(set(plugin_ids)):
            raise PluginValidationError("plugin IDs must be unique")

        capabilities: dict[str, CapabilityContract] = {}
        skills: dict[tuple[str, str], SkillRegistration] = {}
        workflows: dict[tuple[str, str], WorkflowRegistration] = {}
        profiles: dict[tuple[str, str], LoopProfile] = {}
        evaluators: list[OutcomeEvaluator] = []
        services: list[ManagedService] = []
        for contribution in contributions:
            require_version(contribution.sdk_api_version, kind="plugin SDK API", supported_major=SUPPORTED_PLUGIN_API_MAJOR)
            for capability in contribution.capabilities:
                if capability.capability in capabilities:
                    raise PluginValidationError(
                        f"duplicate capability: {capability.capability}"
                    )
                capabilities[capability.capability] = capability
            for skill in contribution.skills:
                key = (skill.manifest.skill_id, skill.manifest.version)
                if key in skills:
                    raise PluginValidationError(f"duplicate skill: {key}")
                skills[key] = skill
            for workflow in contribution.workflows:
                key = (workflow.workflow_id, workflow.version)
                if key in workflows:
                    raise PluginValidationError(f"duplicate workflow: {key}")
                workflows[key] = workflow
            for profile in contribution.loop_profiles:
                key = (profile.profile_id, profile.version)
                if key in profiles:
                    raise PluginValidationError(f"duplicate loop profile: {key}")
                profiles[key] = profile
            evaluators.extend(contribution.evaluators)
            services.extend(contribution.services)

        for skill in skills.values():
            missing = set(skill.manifest.capabilities) - capabilities.keys()
            if missing:
                raise PluginValidationError(
                    f"skill {skill.manifest.skill_id} references unknown capabilities: {sorted(missing)}"
                )
        for workflow in workflows.values():
            missing = set(workflow.required_capabilities) - capabilities.keys()
            if missing:
                raise PluginValidationError(
                    f"workflow {workflow.workflow_id} references unknown capabilities: {sorted(missing)}"
                )
        workflow_ids = {workflow.workflow_id for workflow in workflows.values()}
        for profile in profiles.values():
            if profile.workflow_id not in workflow_ids:
                raise PluginValidationError(
                    f"loop profile {profile.profile_id} references unknown workflow: {profile.workflow_id}"
                )
        service_names = [service.name for service in services]
        if len(service_names) != len(set(service_names)):
            raise PluginValidationError("plugin service names must be unique")

        return cls(
            plugin_ids=tuple(plugin_ids),
            capabilities=MappingProxyType(capabilities),
            skills=MappingProxyType(skills),
            workflows=MappingProxyType(workflows),
            loop_profiles=MappingProxyType(profiles),
            evaluators=tuple(evaluators),
            services=tuple(services),
        )
