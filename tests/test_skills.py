from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from active_agent_platform.foundation.clock import FakeClock
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityContract,
    CapabilityRegistry,
    HealthStatus,
    ResourceBudget,
    SideEffect,
    SkillBinding,
    SkillContext,
    SkillError,
    SkillHealth,
    SkillInvocation,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
    SkillResult,
    SkillStatus,
    schema_compatible,
)

NOW = datetime(2026, 8, 17, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def schema(*, required: tuple[str, ...] = ("value",), kind: str = "string") -> dict[str, object]:
    return {"type": "object", "properties": {"value": {"type": kind}}, "required": list(required), "additionalProperties": False}


def manifest(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "skill_id": "fake-reader",
        "version": "1.2.0",
        "digest": DIGEST,
        "provides": [{"capability": "market.data.read", "capability_version": "1.0"}],
        "side_effect": "PURE",
        "required_permissions": ["market.read"],
        "runtime": "python",
        "entrypoint": "fake:invoke",
        "timeout_seconds": 10,
        "concurrency_limit": 2,
        "supports_cancel": True,
        "resources": {"max_cost": 0.1, "max_latency_ms": 50, "memory_mb": 64},
        "data_regions": ["cn"],
    }
    value.update(updates)
    return value


def registries() -> tuple[CapabilityRegistry, SkillRegistry]:
    capabilities = CapabilityRegistry()
    capabilities.register(CapabilityContract("market.data.read", "1.0", schema(), schema(), SideEffect.PURE, frozenset({"market.read"})))
    return capabilities, SkillRegistry(capabilities)


def test_capability_registry_and_immutable_contract() -> None:
    capabilities, _ = registries()
    item = capabilities.get("market.data.read", "1.0")
    assert item.major == 1 and item.digest.startswith("sha256:")
    with pytest.raises(TypeError):
        item.input_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(SkillError, match="exists"):
        capabilities.register(item)
    with pytest.raises(SkillError) as error:
        capabilities.get("unknown.read", "1.0")
    assert error.value.code == "CAPABILITY_NOT_FOUND"


@pytest.mark.parametrize("name,version", [("Bad", "1.0"), ("market.read", "0.1")])
def test_rejects_invalid_capability(name: str, version: str) -> None:
    with pytest.raises(SkillError) as error:
        CapabilityContract(name, version, schema(), schema(), SideEffect.PURE)
    assert error.value.code == "CAPABILITY_SCHEMA_INVALID"


def test_schema_compatibility_is_conservative() -> None:
    wider = {"type": "object", "properties": {"value": {"type": "string"}, "extra": {"type": "integer"}}, "required": ["value"]}
    assert schema_compatible(schema(), wider)
    assert not schema_compatible(schema(), schema(kind="integer"))


def test_manifest_load_install_digest_and_lifecycle() -> None:
    _, skills = registries()
    installed = skills.install(manifest(), package_digest=DIGEST)
    assert installed.status is SkillStatus.INSTALLED and not installed.verified
    with pytest.raises(SkillError) as error:
        skills.enable("fake-reader", "1.2.0", SkillHealth(HealthStatus.HEALTHY, NOW))
    assert error.value.code == "SKILL_NOT_VERIFIED"
    skills.verify("fake-reader", "1.2.0")
    enabled = skills.enable("fake-reader", "1.2.0", SkillHealth(HealthStatus.HEALTHY, NOW, 7))
    assert enabled.status is SkillStatus.ENABLED
    assert skills.disable("fake-reader", "1.2.0").status is SkillStatus.DISABLED


def test_manifest_rejections() -> None:
    _, skills = registries()
    with pytest.raises(SkillError) as error:
        skills.install(manifest(), package_digest="sha256:" + "b" * 64)
    assert error.value.code == "SKILL_DIGEST_MISMATCH"
    with pytest.raises(SkillError) as error:
        skills.install(manifest(unexpected=True), package_digest=DIGEST)
    assert error.value.code == "SKILL_MANIFEST_INVALID"
    with pytest.raises(SkillError) as error:
        skills.install(manifest(required_permissions=["admin"]), package_digest=DIGEST)
    assert error.value.code == "SKILL_SCHEMA_INCOMPATIBLE"


@pytest.mark.parametrize(
    "update",
    [
        {"skill_id": "Bad"},
        {"provides": []},
        {"provides": ["bad"]},
        {"provides": [{"capability": "bad", "capability_version": "1.0"}]},
        {"required_permissions": ["x", "x"]},
        {"runtime": "shell"},
        {"side_effect": "MAGIC"},
        {"resources": []},
        {"data_regions": [1]},
    ],
)
def test_manifest_rejects_malformed_contract_fields(update: dict[str, object]) -> None:
    value = manifest(**update)
    with pytest.raises(SkillError) as error:
        SkillRegistry(registries()[0]).install(value, package_digest=str(value["digest"]))
    assert error.value.code == "SKILL_MANIFEST_INVALID"


def test_registry_rejects_duplicate_unhealthy_and_unknown() -> None:
    _, skills = registries()
    skills.install(manifest(), package_digest=DIGEST)
    with pytest.raises(SkillError) as error:
        skills.install(manifest(), package_digest=DIGEST)
    assert error.value.code == "SKILL_ALREADY_INSTALLED"
    skills.verify("fake-reader", "1.2.0")
    with pytest.raises(SkillError) as error:
        skills.enable("fake-reader", "1.2.0", SkillHealth(HealthStatus.UNHEALTHY, NOW))
    assert error.value.code == "SKILL_UNHEALTHY"
    with pytest.raises(SkillError) as error:
        skills.get("missing", "1.0.0")
    assert error.value.code == "SKILL_NOT_FOUND"


def enable(skills: SkillRegistry, value: dict[str, object], status: HealthStatus = HealthStatus.HEALTHY) -> None:
    digest = str(value["digest"])
    skills.install(value, package_digest=digest)
    skills.verify(str(value["skill_id"]), str(value["version"]))
    skills.enable(str(value["skill_id"]), str(value["version"]), SkillHealth(status, NOW))


def test_resolver_filters_and_stably_ranks_then_pins() -> None:
    capabilities, skills = registries()
    enable(skills, manifest(skill_id="z-reader", version="2.0.0", digest="sha256:" + "b" * 64, resources={"max_cost": 0.2, "max_latency_ms": 40}))
    enable(skills, manifest(skill_id="a-reader", version="1.3.0", digest="sha256:" + "c" * 64, resources={"max_cost": 0.1, "max_latency_ms": 60}))
    resolver = SkillResolver(capabilities, skills, clock=FakeClock(NOW))
    binding = resolver.resolve(SkillRequirement("fetch", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE, max_cost=0.5, max_latency_ms=100, data_region="cn"), policy_version="policy-3")
    assert binding.skill_id == "a-reader" and binding.skill_version == "1.3.0"
    skills.disable("a-reader", "1.3.0")
    assert binding.skill_id == "a-reader"  # immutable snapshot
    assert resolver.resolve(SkillRequirement("fetch", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE), policy_version="policy-3").skill_id == "z-reader"


def test_resolver_reports_permission_and_no_candidate() -> None:
    capabilities, skills = registries()
    enable(skills, manifest())
    resolver = SkillResolver(capabilities, skills, clock=FakeClock(NOW))
    requirement = SkillRequirement("n", "market.data.read", "1.0", frozenset(), SideEffect.PURE)
    with pytest.raises(SkillError) as error:
        resolver.resolve(requirement, policy_version="p")
    assert error.value.code == "SKILL_PERMISSION_DENIED"
    skills.disable("fake-reader", "1.2.0")
    with pytest.raises(SkillError) as error:
        resolver.resolve(requirement, policy_version="p")
    assert error.value.code == "SKILL_BINDING_NOT_FOUND"


def test_resolver_filters_constraints_and_ranks_health() -> None:
    capabilities, skills = registries()
    enable(skills, manifest(skill_id="degraded", digest="sha256:" + "d" * 64), HealthStatus.DEGRADED)
    resolver = SkillResolver(capabilities, skills, clock=FakeClock(NOW))
    too_strict = SkillRequirement("n", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE, max_cost=0.01)
    with pytest.raises(SkillError) as error:
        resolver.resolve(too_strict, policy_version="p")
    assert error.value.code == "SKILL_BINDING_NOT_FOUND"
    binding = resolver.resolve(SkillRequirement("n", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE), policy_version="p")
    assert binding.skill_id == "degraded"


def test_contract_and_binding_validate_boundaries() -> None:
    with pytest.raises(SkillError):
        CapabilityContract("market.data.read", "1.0", {"type": "array"}, schema(), SideEffect.PURE)
    with pytest.raises(SkillError):
        CapabilityContract("market.data.read", "1.0", schema(), schema(), SideEffect.PURE, frozenset({""}))
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        SkillBinding("n", "market.data.read", "1.0", "fake", "1.0.0", DIGEST, "p", naive)
    token = CancellationToken()
    assert not token.cancelled
    token.cancel()
    assert token.cancelled


class Logger:
    def info(self, message: str, **fields: object) -> None:
        pass


class Artifacts:
    async def write(self, content: bytes) -> str:
        return DIGEST


class Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        self.calls += 1
        return SkillResult("SUCCEEDED", {"value": "ok"})

    async def health(self) -> SkillHealth:
        return SkillHealth(HealthStatus.HEALTHY, NOW)

    async def cancel(self, invocation_id: str) -> str:
        return "CANCELLED"

    async def query_result(self, idempotency_key: str, provider_operation_id: str | None) -> SkillResult:
        return SkillResult("UNKNOWN")


def invocation(binding: SkillBinding, *, deadline: datetime = NOW + timedelta(seconds=1), permissions: frozenset[str] = frozenset({"market.read"})) -> SkillInvocation:
    return SkillInvocation("i", "t", "r", "n", binding, {"value": "x"}, deadline, "key", 1, permissions, ResourceBudget(1))


@pytest.mark.asyncio
async def test_invoker_enforces_binding_permission_deadline_and_context_isolation() -> None:
    capabilities, skills = registries()
    enable(skills, manifest())
    binding = SkillResolver(capabilities, skills, clock=FakeClock(NOW)).resolve(SkillRequirement("n", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE), policy_version="p")
    adapter = Adapter()
    invoker = SkillInvoker(skills, {("fake-reader", "1.2.0"): adapter})
    context = SkillContext(FakeClock(NOW), Logger(), CancellationToken(), Artifacts(), {"api": "secret-ref"}, ResourceBudget(1))
    result = await invoker.invoke(invocation(binding), context)
    assert result.status == "SUCCEEDED" and adapter.calls == 1
    assert await invoker.cancel(binding, "i") == "CANCELLED"
    with pytest.raises(SkillError) as error:
        await invoker.query_result(binding, "key", None)
    assert error.value.code == "SKILL_RECOVERY_UNKNOWN"
    with pytest.raises(SkillError) as error:
        await invoker.invoke(invocation(binding, permissions=frozenset()), context)
    assert error.value.code == "SKILL_PERMISSION_DENIED" and adapter.calls == 1
    with pytest.raises(SkillError) as error:
        await invoker.invoke(invocation(binding, deadline=NOW), context)
    assert error.value.code == "SKILL_TIMEOUT" and adapter.calls == 1
    with pytest.raises(TypeError):
        context.secret_refs["raw"] = "no"  # type: ignore[index]


@pytest.mark.asyncio
async def test_invoker_rejects_digest_drift_and_missing_adapter() -> None:
    capabilities, skills = registries()
    enable(skills, manifest())
    binding = SkillResolver(capabilities, skills, clock=FakeClock(NOW)).resolve(SkillRequirement("n", "market.data.read", "1.0", frozenset({"market.read"}), SideEffect.PURE), policy_version="p")
    context = SkillContext(FakeClock(NOW), Logger(), CancellationToken(), Artifacts(), {}, ResourceBudget(1))
    with pytest.raises(SkillError) as error:
        await SkillInvoker(skills, {}).invoke(invocation(binding), context)
    assert error.value.code == "SKILL_BINDING_NOT_FOUND"
    with pytest.raises(SkillError) as error:
        await SkillInvoker(skills, {}).cancel(binding, "i")
    assert error.value.code == "SKILL_BINDING_NOT_FOUND"
    drifted = SkillBinding(binding.node_id, binding.capability, binding.capability_version, binding.skill_id, binding.skill_version, "sha256:" + "f" * 64, binding.binding_policy_version, binding.resolved_at)
    with pytest.raises(SkillError) as error:
        await SkillInvoker(skills, {}).invoke(invocation(drifted), context)
    assert error.value.code == "SKILL_BINDING_NOT_FOUND"
