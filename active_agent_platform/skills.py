"""Capability contracts, governed Skill lifecycle, invocation boundary and resolver."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Protocol

JsonObject = Mapping[str, object]
_CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")
_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[0-9]+)\.(0|[0-9]+)$")
_CAP_VERSION = re.compile(r"^([1-9][0-9]*)\.([0-9]+)$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class SkillError(ValueError):
    """Stable, branchable failure raised by the Skill control plane."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SideEffect(StrEnum):
    PURE = "PURE"
    IDEMPOTENT = "IDEMPOTENT"
    QUERYABLE = "QUERYABLE"
    NON_REPLAYABLE = "NON_REPLAYABLE"


class SkillStatus(StrEnum):
    INSTALLED = "INSTALLED"
    VERIFIED = "VERIFIED"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


class _Risk(IntEnum):
    PURE = 0
    IDEMPOTENT = 1
    QUERYABLE = 2
    NON_REPLAYABLE = 3


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("value must be JSON-compatible")


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability: str
    version: str
    input_schema: JsonObject
    output_schema: JsonObject
    side_effect: SideEffect
    allowed_permissions: frozenset[str] = frozenset()
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if _CAPABILITY.fullmatch(self.capability) is None or _CAP_VERSION.fullmatch(self.version) is None:
            raise SkillError("CAPABILITY_SCHEMA_INVALID", "invalid capability name or version")
        _validate_schema(self.input_schema)
        _validate_schema(self.output_schema)
        if any(not permission for permission in self.allowed_permissions):
            raise SkillError("CAPABILITY_SCHEMA_INVALID", "permissions must be non-empty")
        canonical = {
            "capability": self.capability,
            "version": self.version,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "side_effect": self.side_effect.value,
            "allowed_permissions": sorted(self.allowed_permissions),
        }
        object.__setattr__(self, "digest", _digest(canonical))
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))

    @property
    def major(self) -> int:
        return int(self.version.split(".", 1)[0])


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], CapabilityContract] = {}

    def register(self, contract: CapabilityContract) -> CapabilityContract:
        key = (contract.capability, contract.version)
        if key in self._items:
            raise SkillError("CAPABILITY_ALREADY_EXISTS", f"capability exists: {contract.capability}@{contract.version}")
        self._items[key] = contract
        return contract

    def get(self, capability: str, version: str) -> CapabilityContract:
        try:
            return self._items[(capability, version)]
        except KeyError as error:
            raise SkillError("CAPABILITY_NOT_FOUND", f"capability not found: {capability}@{version}") from error

    def compatible(self, required: CapabilityContract, provided: CapabilityContract) -> bool:
        return (
            required.capability == provided.capability
            and required.major == provided.major
            and schema_compatible(required.input_schema, provided.input_schema, contravariant=True)
            and schema_compatible(required.output_schema, provided.output_schema)
            and _Risk[provided.side_effect.name] <= _Risk[required.side_effect.name]
            and provided.allowed_permissions <= required.allowed_permissions
        )


@dataclass(frozen=True, slots=True)
class CapabilityProvision:
    capability: str
    capability_version: str


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_cost: float = 0.0
    max_latency_ms: int = 30_000
    memory_mb: int = 256


@dataclass(frozen=True, slots=True)
class SkillManifest:
    skill_id: str
    version: str
    digest: str
    provides: tuple[CapabilityProvision, ...]
    side_effect: SideEffect
    required_permissions: frozenset[str]
    runtime: str
    entrypoint: str
    timeout_seconds: int
    concurrency_limit: int
    supports_cancel: bool = False
    supports_query: bool = False
    data_regions: frozenset[str] = frozenset()
    resources: ResourceLimits = ResourceLimits()

    @classmethod
    def load(cls, value: Mapping[str, object], *, package_digest: str | None = None) -> SkillManifest:
        required = {"schema_version", "skill_id", "version", "digest", "provides", "side_effect", "required_permissions", "runtime", "entrypoint", "timeout_seconds", "concurrency_limit"}
        allowed = required | {"supports_cancel", "supports_query", "data_regions", "resources", "healthcheck"}
        if set(value) - allowed or not required <= set(value) or value.get("schema_version") != "1.0":
            raise SkillError("SKILL_MANIFEST_INVALID", "manifest fields or schema_version are invalid")
        skill_id, version, digest = value["skill_id"], value["version"], value["digest"]
        if not isinstance(skill_id, str) or _SKILL_ID.fullmatch(skill_id) is None or not isinstance(version, str) or _SEMVER.fullmatch(version) is None or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise SkillError("SKILL_MANIFEST_INVALID", "invalid skill identity, version or digest")
        if package_digest is not None and digest != package_digest:
            raise SkillError("SKILL_DIGEST_MISMATCH", "manifest digest does not match package")
        raw_provides = value["provides"]
        if not isinstance(raw_provides, list) or not raw_provides:
            raise SkillError("SKILL_MANIFEST_INVALID", "provides must be non-empty")
        provisions: list[CapabilityProvision] = []
        for item in raw_provides:
            if not isinstance(item, Mapping) or set(item) != {"capability", "capability_version"}:
                raise SkillError("SKILL_MANIFEST_INVALID", "invalid capability provision")
            capability, cap_version = item["capability"], item["capability_version"]
            if not isinstance(capability, str) or _CAPABILITY.fullmatch(capability) is None or not isinstance(cap_version, str) or _CAP_VERSION.fullmatch(cap_version) is None:
                raise SkillError("SKILL_MANIFEST_INVALID", "invalid capability provision")
            provisions.append(CapabilityProvision(capability, cap_version))
        permissions = value["required_permissions"]
        if not isinstance(permissions, list) or any(not isinstance(p, str) or not p for p in permissions) or len(set(permissions)) != len(permissions):
            raise SkillError("SKILL_MANIFEST_INVALID", "required_permissions must be unique strings")
        runtime, entrypoint = value["runtime"], value["entrypoint"]
        timeout, concurrency = value["timeout_seconds"], value["concurrency_limit"]
        if runtime not in {"python", "process", "http"} or not isinstance(entrypoint, str) or not entrypoint or not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1 or not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise SkillError("SKILL_MANIFEST_INVALID", "invalid runtime limits")
        try:
            side_effect = SideEffect(str(value["side_effect"]))
        except ValueError as error:
            raise SkillError("SKILL_MANIFEST_INVALID", "invalid side effect") from error
        resources = value.get("resources", {})
        if not isinstance(resources, Mapping):
            raise SkillError("SKILL_MANIFEST_INVALID", "resources must be an object")
        limits = ResourceLimits(float(resources.get("max_cost", 0)), int(resources.get("max_latency_ms", timeout * 1000)), int(resources.get("memory_mb", 256)))
        regions = value.get("data_regions", [])
        if not isinstance(regions, list) or any(not isinstance(x, str) for x in regions):
            raise SkillError("SKILL_MANIFEST_INVALID", "data_regions must be strings")
        return cls(skill_id, version, digest, tuple(provisions), side_effect, frozenset(permissions), str(runtime), entrypoint, timeout, concurrency, bool(value.get("supports_cancel", False)), bool(value.get("supports_query", False)), frozenset(regions), limits)


@dataclass(frozen=True, slots=True)
class SkillHealth:
    status: HealthStatus
    checked_at: datetime
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class InstalledSkill:
    manifest: SkillManifest
    status: SkillStatus
    verified: bool
    health: SkillHealth | None = None


class SkillRegistry:
    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities
        self._items: dict[tuple[str, str], InstalledSkill] = {}

    def install(self, value: Mapping[str, object], *, package_digest: str) -> InstalledSkill:
        manifest = SkillManifest.load(value, package_digest=package_digest)
        key = (manifest.skill_id, manifest.version)
        if key in self._items:
            raise SkillError("SKILL_ALREADY_INSTALLED", f"skill already installed: {manifest.skill_id}@{manifest.version}")
        for provision in manifest.provides:
            contract = self._capabilities.get(provision.capability, provision.capability_version)
            if manifest.side_effect != contract.side_effect or not manifest.required_permissions <= contract.allowed_permissions:
                raise SkillError("SKILL_SCHEMA_INCOMPATIBLE", "manifest exceeds capability contract")
        item = InstalledSkill(manifest, SkillStatus.INSTALLED, False)
        self._items[key] = item
        return item

    def verify(self, skill_id: str, version: str) -> InstalledSkill:
        item = self.get(skill_id, version)
        updated = replace(item, status=SkillStatus.VERIFIED, verified=True)
        self._items[(skill_id, version)] = updated
        return updated

    def enable(self, skill_id: str, version: str, health: SkillHealth) -> InstalledSkill:
        item = self.get(skill_id, version)
        if not item.verified:
            raise SkillError("SKILL_NOT_VERIFIED", "unverified skill cannot be enabled")
        if health.status is HealthStatus.UNHEALTHY:
            raise SkillError("SKILL_UNHEALTHY", "unhealthy skill cannot be enabled")
        updated = replace(item, status=SkillStatus.ENABLED, health=health)
        self._items[(skill_id, version)] = updated
        return updated

    def disable(self, skill_id: str, version: str) -> InstalledSkill:
        item = self.get(skill_id, version)
        updated = replace(item, status=SkillStatus.DISABLED)
        self._items[(skill_id, version)] = updated
        return updated

    def get(self, skill_id: str, version: str) -> InstalledSkill:
        try:
            return self._items[(skill_id, version)]
        except KeyError as error:
            raise SkillError("SKILL_NOT_FOUND", f"skill not found: {skill_id}@{version}") from error

    def candidates(self, capability: str) -> tuple[InstalledSkill, ...]:
        return tuple(item for item in self._items.values() if item.status is SkillStatus.ENABLED and any(p.capability == capability for p in item.manifest.provides))


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class LoggerPort(Protocol):
    def info(self, message: str, **fields: object) -> None: ...


class ArtifactWriter(Protocol):
    async def write(self, content: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    max_cost: float
    max_output_bytes: int = 1_048_576


@dataclass(frozen=True, slots=True)
class SkillContext:
    clock: ClockPort
    logger: LoggerPort
    cancellation: CancellationToken
    artifact_writer: ArtifactWriter
    secret_refs: Mapping[str, str]
    budget: ResourceBudget

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret_refs", MappingProxyType(dict(self.secret_refs)))


@dataclass(frozen=True, slots=True)
class SkillBinding:
    node_id: str
    capability: str
    capability_version: str
    skill_id: str
    skill_version: str
    skill_digest: str
    binding_policy_version: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        if self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at must be timezone-aware")
        object.__setattr__(self, "resolved_at", self.resolved_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    invocation_id: str
    task_id: str
    run_id: str
    node_id: str
    binding: SkillBinding
    input: JsonObject
    deadline: datetime
    idempotency_key: str
    attempt: int
    allowed_permissions: frozenset[str]
    budget: ResourceBudget


@dataclass(frozen=True, slots=True)
class SkillResult:
    status: str
    output: object | None = None
    provider_operation_id: str | None = None


class SkillAdapter(Protocol):
    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult: ...
    async def health(self) -> SkillHealth: ...
    async def cancel(self, invocation_id: str) -> str: ...
    async def query_result(self, idempotency_key: str, provider_operation_id: str | None) -> SkillResult: ...


@dataclass(frozen=True, slots=True)
class SkillRequirement:
    node_id: str
    capability: str
    capability_version: str
    allowed_permissions: frozenset[str]
    side_effect: SideEffect
    max_cost: float | None = None
    max_latency_ms: int | None = None
    data_region: str | None = None


class SkillResolver:
    def __init__(self, capabilities: CapabilityRegistry, skills: SkillRegistry, *, clock: ClockPort) -> None:
        self._capabilities, self._skills, self._clock = capabilities, skills, clock

    def resolve(self, requirement: SkillRequirement, *, policy_version: str) -> SkillBinding:
        required = self._capabilities.get(requirement.capability, requirement.capability_version)
        ranked: list[tuple[tuple[object, ...], InstalledSkill]] = []
        saw_permission = False
        for item in self._skills.candidates(requirement.capability):
            manifest = item.manifest
            provision = next(p for p in manifest.provides if p.capability == requirement.capability)
            try:
                provided = self._capabilities.get(provision.capability, provision.capability_version)
            except SkillError:
                continue
            if not self._capabilities.compatible(required, provided) or _Risk[manifest.side_effect.name] > _Risk[requirement.side_effect.name]:
                continue
            if not manifest.required_permissions <= requirement.allowed_permissions:
                saw_permission = True
                continue
            if item.health is None or item.health.status is HealthStatus.UNHEALTHY:
                continue
            limits = manifest.resources
            if requirement.max_cost is not None and limits.max_cost > requirement.max_cost or requirement.max_latency_ms is not None and limits.max_latency_ms > requirement.max_latency_ms or requirement.data_region is not None and manifest.data_regions and requirement.data_region not in manifest.data_regions:
                continue
            health_rank = 0 if item.health.status is HealthStatus.HEALTHY else 1
            version_rank = tuple(-int(x) for x in manifest.version.split("."))
            ranked.append(((health_rank, limits.max_cost, limits.max_latency_ms, version_rank, manifest.skill_id, manifest.digest), item))
        if not ranked:
            code = "SKILL_PERMISSION_DENIED" if saw_permission else "SKILL_BINDING_NOT_FOUND"
            raise SkillError(code, f"no eligible skill for {requirement.capability}@{requirement.capability_version}")
        chosen = min(ranked, key=lambda pair: pair[0])[1].manifest
        return SkillBinding(requirement.node_id, requirement.capability, requirement.capability_version, chosen.skill_id, chosen.version, chosen.digest, policy_version, self._clock.now())


class SkillInvoker:
    """Checks a pinned binding before crossing the minimal adapter boundary."""

    def __init__(self, registry: SkillRegistry, adapters: Mapping[tuple[str, str], SkillAdapter]) -> None:
        self._registry, self._adapters = registry, dict(adapters)

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        item = self._registry.get(invocation.binding.skill_id, invocation.binding.skill_version)
        if item.manifest.digest != invocation.binding.skill_digest:
            raise SkillError("SKILL_BINDING_NOT_FOUND", "binding digest no longer identifies the installed skill")
        if not item.manifest.required_permissions <= invocation.allowed_permissions:
            raise SkillError("SKILL_PERMISSION_DENIED", "invocation permissions are insufficient")
        if context.clock.now() >= invocation.deadline:
            raise SkillError("SKILL_TIMEOUT", "invocation deadline has passed")
        adapter = self._adapters.get((item.manifest.skill_id, item.manifest.version))
        if adapter is None:
            raise SkillError("SKILL_BINDING_NOT_FOUND", "adapter is not installed")
        return await adapter.invoke(invocation, context)

    async def cancel(self, binding: SkillBinding, invocation_id: str) -> str:
        """Cancel an invocation through the adapter pinned by the original binding."""
        adapter = self._adapters.get((binding.skill_id, binding.skill_version))
        if adapter is None:
            raise SkillError("SKILL_BINDING_NOT_FOUND", "adapter is not installed")
        return await adapter.cancel(invocation_id)

    async def query_result(
        self,
        binding: SkillBinding,
        idempotency_key: str,
        provider_operation_id: str | None,
    ) -> SkillResult:
        """Query the adapter pinned by a binding without resolving a replacement Skill."""
        item = self._registry.get(binding.skill_id, binding.skill_version)
        if item.manifest.digest != binding.skill_digest:
            raise SkillError("SKILL_BINDING_NOT_FOUND", "binding digest no longer identifies the installed skill")
        if not item.manifest.supports_query:
            raise SkillError("SKILL_RECOVERY_UNKNOWN", "skill does not support result queries")
        adapter = self._adapters.get((binding.skill_id, binding.skill_version))
        if adapter is None:
            raise SkillError("SKILL_BINDING_NOT_FOUND", "adapter is not installed")
        return await adapter.query_result(idempotency_key, provider_operation_id)


def _validate_schema(schema: JsonObject) -> None:
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise SkillError("CAPABILITY_SCHEMA_INVALID", "I/O schema must be an object schema")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, Sequence) or isinstance(required, str | bytes) or any(not isinstance(x, str) or x not in properties for x in required):
        raise SkillError("CAPABILITY_SCHEMA_INVALID", "invalid schema properties or required fields")


def schema_compatible(required: JsonObject, provided: JsonObject, *, contravariant: bool = False) -> bool:
    """Conservative object-schema compatibility for capability I/O contracts."""
    _validate_schema(required)
    _validate_schema(provided)
    required_props = required.get("properties", {})
    provided_props = provided.get("properties", {})
    assert isinstance(required_props, Mapping) and isinstance(provided_props, Mapping)
    required_raw, provided_raw = required.get("required", []), provided.get("required", [])
    assert isinstance(required_raw, Sequence) and isinstance(provided_raw, Sequence)
    required_names = {str(name) for name in required_raw}
    provided_names = {str(name) for name in provided_raw}
    if contravariant and not provided_names <= required_names:
        return False
    if not contravariant and not required_names <= provided_names:
        return False
    for name, required_rule in required_props.items():
        provided_rule = provided_props.get(name)
        if not isinstance(provided_rule, Mapping) or not isinstance(required_rule, Mapping):
            return False
        if provided_rule.get("type") != required_rule.get("type"):
            return False
        if "enum" in required_rule:
            provided_enum, required_enum = provided_rule.get("enum"), required_rule["enum"]
            if (
                not isinstance(provided_enum, Sequence)
                or isinstance(provided_enum, str | bytes)
                or not isinstance(required_enum, Sequence)
                or isinstance(required_enum, str | bytes)
                or not set(provided_enum) <= set(required_enum)
            ):
                return False
    return True
