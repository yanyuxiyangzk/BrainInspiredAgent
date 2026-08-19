"""Stable contracts exposed to domain plugins."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias

from brain_kernel.lifecycle import ManagedService

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class SideEffect(StrEnum):
    PURE = "PURE"
    IDEMPOTENT = "IDEMPOTENT"
    QUERYABLE = "QUERYABLE"
    NON_REPLAYABLE = "NON_REPLAYABLE"


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability: str
    version: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    side_effect: SideEffect = SideEffect.PURE

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+", self.capability) is None:
            raise ValueError("capability must be a dotted lowercase identifier")
        _validate_version(self.version)


@dataclass(frozen=True, slots=True)
class SkillManifest:
    skill_id: str
    version: str
    digest: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{2,63}", self.skill_id) is None:
            raise ValueError("skill_id is invalid")
        _validate_version(self.version)
        if not self.digest.startswith("sha256:") or len(self.digest) <= len("sha256:"):
            raise ValueError("digest must use the sha256 scheme")
        if not self.capabilities or len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be non-empty and unique")


class SkillAdapter(Protocol):
    async def invoke(self, input_data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class SkillRegistration:
    manifest: SkillManifest
    adapter: SkillAdapter


@dataclass(frozen=True, slots=True)
class WorkflowRegistration:
    workflow_id: str
    version: str
    definition: Mapping[str, object]
    required_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.workflow_id) is None:
            raise ValueError("workflow_id is invalid")
        _validate_version(self.version)
        if not self.required_capabilities:
            raise ValueError("required_capabilities must not be empty")


@dataclass(frozen=True, slots=True)
class LoopProfile:
    profile_id: str
    version: str
    workflow_id: str
    trigger: str
    checkpoint_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.profile_id or not self.workflow_id or not self.trigger:
            raise ValueError("loop profile identifiers and trigger must not be empty")
        _validate_version(self.version)


class OutcomeEvaluator(Protocol):
    async def evaluate(
        self, output: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class PluginContribution:
    plugin_id: str
    capabilities: tuple[CapabilityContract, ...] = ()
    skills: tuple[SkillRegistration, ...] = ()
    workflows: tuple[WorkflowRegistration, ...] = ()
    loop_profiles: tuple[LoopProfile, ...] = ()
    evaluators: tuple[OutcomeEvaluator, ...] = ()
    services: tuple[ManagedService, ...] = ()
    sdk_api_version: str = "1.0"

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", self.plugin_id) is None:
            raise ValueError("plugin_id is invalid")


class DomainPlugin(Protocol):
    def contribute(self) -> PluginContribution: ...


def _validate_version(version: str) -> None:
    if re.fullmatch(r"[1-9][0-9]*\.[0-9]+(?:\.[0-9]+)?", version) is None:
        raise ValueError("version must be a positive major semantic version")
