from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.skills import (
    CancellationToken,
    CapabilityRegistry,
    ResourceBudget,
    SideEffect,
    SkillContext,
    SkillError,
    SkillInvocation,
    SkillRegistry,
    SkillRequirement,
    SkillResolver,
)
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import ExpressionError, WorkflowValidator, resolve_json_path
from apps.quant_agent import (
    MARKET_CAPABILITY,
    MARKET_SUMMARY_WORKFLOW,
    NOTIFICATION_CAPABILITY,
    SUMMARY_CAPABILITY,
    install_fake_skills,
)

NOW = datetime(2026, 8, 17, 1, 25, tzinfo=UTC)


class Logger:
    def info(self, message: str, **fields: object) -> None:
        del message, fields


class Artifacts:
    async def write(self, content: bytes) -> str:
        del content
        return "sha256:" + "0" * 64


def context(clock: FakeClock) -> SkillContext:
    return SkillContext(clock, Logger(), CancellationToken(), Artifacts(), {}, ResourceBudget(1))


def call(binding: object, node: str, value: dict[str, object], key: str):
    from active_agent_platform.skills import SkillBinding

    assert isinstance(binding, SkillBinding)
    return SkillInvocation(
        f"invoke-{node}", "task-golden", "run-golden", node, binding, value,
        NOW + timedelta(seconds=10), key, 1,
        frozenset({"market.read", "notification.local.write"}), ResourceBudget(1),
    )


def resolve(
    resolver: SkillResolver, node: str, capability: str, side_effect: SideEffect,
):
    return resolver.resolve(
        SkillRequirement(
            node, capability, "1.0",
            frozenset({"market.read", "notification.local.write"}), side_effect,
        ),
        policy_version="t03-golden/1.0",
    )


def test_market_summary_workflow_golden_contract() -> None:
    validation = WorkflowValidator().validate(MARKET_SUMMARY_WORKFLOW)
    assert validation.workflow_id == "market_summary"
    assert validation.version == "1.0.0"
    assert validation.topological_order == ("read_snapshot", "build_summary", "notify")
    assert MARKET_SUMMARY_WORKFLOW["output_mapping"] == {
        "summary": "$.nodes.build_summary.output.summary",
        "item_count": "$.nodes.build_summary.output.item_count",
        "notification_id": "$.nodes.notify.output.notification_id",
        "delivered": "$.nodes.notify.output.delivered",
    }


@pytest.mark.asyncio
async def test_three_skills_produce_stable_market_summary_golden_output(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "golden.db")
    await database.initialize()
    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock, database=database)
    resolver = SkillResolver(capabilities, skills, clock=clock)
    ctx = context(clock)

    read_binding = resolve(resolver, "read_snapshot", MARKET_CAPABILITY, SideEffect.PURE)
    snapshot = await bundle.market.invoke(
        call(read_binding, "read_snapshot", {"symbols": ["INDEX.TEST"], "trade_date": "2026-08-17"}, "read:2026-08-17"), ctx
    )
    assert snapshot.output == {
        "trade_date": "2026-08-17",
        "quotes": [{
            "symbol": "INDEX.TEST", "price": 24.448, "volume": 819448,
            "as_of": "2026-08-17T07:00:00Z",
        }],
    }
    summary_binding = resolve(resolver, "build_summary", SUMMARY_CAPABILITY, SideEffect.PURE)
    summary = await bundle.summary.invoke(
        call(summary_binding, "build_summary", {"title": "Auction", "items": snapshot.output["quotes"], "max_items": 10}, "summary:2026-08-17"), ctx  # type: ignore[index]
    )
    assert summary.output == {
        "summary": "Auction: {'symbol': 'INDEX.TEST', 'price': 24.448, 'volume': 819448, 'as_of': '2026-08-17T07:00:00Z'}",
        "item_count": 1,
    }
    notification_binding = resolve(
        resolver, "notify", NOTIFICATION_CAPABILITY, SideEffect.IDEMPOTENT
    )
    notification = await bundle.notification.invoke(
        call(
            notification_binding, "notify",
            {"title": "Auction", "message": summary.output["summary"], "level": "INFO"},  # type: ignore[index]
            "market_summary:2026-08-17:auction",
        ),
        ctx,
    )
    assert notification.status == "SUCCEEDED"
    assert notification.output == {
        "notification_id": "55a6cbb520b6fcf9e7bb47a4",
        "delivered": True,
    }


def test_illegal_reference_and_resource_limit_are_rejected() -> None:
    with pytest.raises(ExpressionError):
        resolve_json_path({"params": {"symbols": ["INDEX.TEST"]}}, "$.params.symbols[0]")

    clock = FakeClock(NOW)
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    install_fake_skills(capabilities, skills, clock=clock)
    with pytest.raises(SkillError) as limited:
        SkillResolver(capabilities, skills, clock=clock).resolve(
            SkillRequirement(
                "read", MARKET_CAPABILITY, "1.0", frozenset({"market.read"}),
                SideEffect.PURE, max_latency_ms=1,
            ),
            policy_version="t03",
        )
    assert limited.value.code == "SKILL_BINDING_NOT_FOUND"
