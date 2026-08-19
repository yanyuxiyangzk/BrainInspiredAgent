from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.foundation.clock import FakeClock
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillInvocation,
    SkillInvoker,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
    SkillResult,
)
from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.fake_skills import (
    MARKET_CAPABILITY,
    NOTIFICATION_CAPABILITY,
    SUMMARY_CAPABILITY,
    FakeMarketRead,
    LocalNotification,
    install_fake_skills,
)

NOW = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)


class Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def info(self, message: str, **fields: object) -> None:
        self.messages.append((message, fields))


class Artifacts:
    async def write(self, content: bytes) -> str:
        return "sha256:" + "0" * 64


def composition() -> tuple[FakeClock, CapabilityRegistry, SkillRegistry, object]:
    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock)
    return clock, capabilities, skills, bundle


def invocation(
    binding: object,
    input_value: dict[str, object],
    *,
    key: str = "test-key",
    token: CancellationToken | None = None,
) -> tuple[SkillInvocation, SkillContext, Logger]:
    from active_agent_platform.skills import SkillBinding

    assert isinstance(binding, SkillBinding)
    logger = Logger()
    context = SkillContext(
        FakeClock(NOW),
        logger,
        token or CancellationToken(),
        Artifacts(),
        {},
        ResourceBudget(1),
    )
    call = SkillInvocation(
        "invocation-1",
        "task-1",
        "run-1",
        binding.node_id,
        binding,
        input_value,
        NOW + timedelta(minutes=1),
        key,
        1,
        frozenset({"market.read", "notification.local.write"}),
        ResourceBudget(1),
    )
    return call, context, logger


def resolve(
    clock: FakeClock,
    capabilities: CapabilityRegistry,
    skills: SkillRegistry,
    capability: str,
    side_effect: SideEffect,
) -> object:
    return SkillResolver(capabilities, skills, clock=clock).resolve(
        SkillRequirement(
            "node",
            capability,
            "1.0",
            frozenset({"market.read", "notification.local.write"}),
            side_effect,
        ),
        policy_version="fake-1",
    )


def test_install_fake_skills_is_ready_for_resolution() -> None:
    clock, capabilities, skills, bundle = composition()
    assert skills.get("fake-market-read", "1.0.0").status == "ENABLED"
    assert skills.get("fake-summary", "1.0.0").status == "ENABLED"
    assert skills.get("local-notification", "1.0.0").status == "ENABLED"
    assert len(bundle.adapters) == 3
    assert resolve(clock, capabilities, skills, MARKET_CAPABILITY, SideEffect.PURE).skill_id == "fake-market-read"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_fake_market_read_is_deterministic_and_validates_input() -> None:
    clock, capabilities, skills, bundle = composition()
    binding = resolve(clock, capabilities, skills, MARKET_CAPABILITY, SideEffect.PURE)
    call, context, _ = invocation(binding, {"symbols": ["000001.SZ"], "trade_date": "2026-08-17"})
    invoker = SkillInvoker(skills, bundle.adapters)
    first = await invoker.invoke(call, context)
    second = await invoker.invoke(call, context)
    assert first == second and first.status == "SUCCEEDED"
    assert first.output["quotes"][0]["symbol"] == "000001.SZ"  # type: ignore[index]
    invalid, context, _ = invocation(binding, {"symbols": []})
    assert (await invoker.invoke(invalid, context)).status == "FAILED"
    assert isinstance(bundle.market, FakeMarketRead) and bundle.market.invocation_count == 3


@pytest.mark.asyncio
async def test_fake_summary_and_cancellation() -> None:
    clock, capabilities, skills, bundle = composition()
    binding = resolve(clock, capabilities, skills, SUMMARY_CAPABILITY, SideEffect.PURE)
    call, context, _ = invocation(
        binding,
        {"title": "Signals", "items": ["alpha", "beta", "gamma"], "max_items": 2},
    )
    result = await SkillInvoker(skills, bundle.adapters).invoke(call, context)
    assert result.output == {"summary": "Signals: alpha; beta", "item_count": 3}
    await bundle.summary.cancel("invocation-1")
    assert (await bundle.summary.invoke(call, context)).status == "CANCELLED"
    assert (await bundle.summary.query_result("missing", None)).status == "UNKNOWN"


@pytest.mark.asyncio
async def test_local_notification_is_idempotent_and_queryable() -> None:
    clock, capabilities, skills, bundle = composition()
    binding = resolve(clock, capabilities, skills, NOTIFICATION_CAPABILITY, SideEffect.IDEMPOTENT)
    call, context, logger = invocation(
        binding,
        {"title": "Done", "message": "Workflow completed", "level": "INFO"},
        key="notify-once",
    )
    invoker = SkillInvoker(skills, bundle.adapters)
    first = await invoker.invoke(call, context)
    second = await invoker.invoke(call, context)
    queried = await invoker.query_result(binding, "notify-once", first.provider_operation_id)
    assert first == second == queried
    assert isinstance(bundle.notification, LocalNotification)
    assert len(bundle.notification.records) == 1
    assert await bundle.notification.persisted_records() == bundle.notification.records
    assert (await bundle.notification.query_result("missing", None)).status == "UNKNOWN"
    assert len(logger.messages) == 1
    invalid, context, _ = invocation(binding, {"title": "", "message": "x"}, key="bad")
    assert (await invoker.invoke(invalid, context)).status == "FAILED"


@pytest.mark.asyncio
async def test_local_notification_survives_restart_without_duplicate_delivery(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "notifications.db")
    await database.initialize()
    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    first_bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    binding = resolve(clock, capabilities, skills, NOTIFICATION_CAPABILITY, SideEffect.IDEMPOTENT)
    call, context, _ = invocation(
        binding, {"title": "Done", "message": "Persisted", "level": "INFO"}, key="stable:task:notice"
    )
    first = await first_bundle.notification.invoke(call, context)
    assert first.status == "SUCCEEDED"

    restarted = LocalNotification(clock=clock, database=database)
    replayed = await restarted.invoke(call, context)
    queried = await restarted.query_result("stable:task:notice", first.provider_operation_id)
    assert replayed == queried == first
    assert restarted.records == ()
    persisted = await restarted.persisted_records()
    assert len(persisted) == 1 and persisted[0].message == "Persisted"
    rows = await database.fetch_all("SELECT * FROM local_notification_delivery")
    assert len(rows) == 1 and rows[0]["idempotency_key"] == "stable:task:notice"


@pytest.mark.asyncio
async def test_notification_same_key_with_different_payload_is_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "conflict.db")
    await database.initialize()
    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    binding = resolve(clock, capabilities, skills, NOTIFICATION_CAPABILITY, SideEffect.IDEMPOTENT)
    first, context, _ = invocation(binding, {"title": "One", "message": "Original"}, key="same-key")
    changed, changed_context, _ = invocation(binding, {"title": "Two", "message": "Changed"}, key="same-key")
    assert (await bundle.notification.invoke(first, context)).status == "SUCCEEDED"
    conflict = await bundle.notification.invoke(changed, changed_context)
    assert conflict == SkillResult("FAILED", {"code": "IDEMPOTENCY_CONFLICT"})
    assert len(await bundle.notification.persisted_records()) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_commits_exactly_one_notification(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "concurrent.db")
    await database.initialize()
    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    binding = resolve(clock, capabilities, skills, NOTIFICATION_CAPABILITY, SideEffect.IDEMPOTENT)
    call, context, _ = invocation(binding, {"title": "Once", "message": "Concurrent"}, key="race-key")
    results = await asyncio.gather(
        bundle.notification.invoke(call, context), bundle.notification.invoke(call, context)
    )
    assert results[0] == results[1]
    assert len(await bundle.notification.persisted_records()) == 1
