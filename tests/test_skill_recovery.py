from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from active_agent_platform.skill_recovery import (
    RecoveryAction,
    SkillRecoveryManager,
    SkillRecoveryRequest,
)
from active_agent_platform.skills import SideEffect, SkillBinding, SkillError, SkillResult

NOW = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        del seconds


class Invoker:
    def __init__(self, result: SkillResult | SkillError) -> None:
        self.result = result
        self.queries: list[tuple[str, str | None]] = []

    async def query_result(
        self, binding: SkillBinding, key: str, operation: str | None
    ) -> SkillResult:
        del binding
        self.queries.append((key, operation))
        if isinstance(self.result, SkillError):
            raise self.result
        return self.result


def binding() -> SkillBinding:
    return SkillBinding(
        "node", "report.send", "1.0", "sender", "1.0.0",
        "sha256:" + "a" * 64, "policy", NOW,
    )


def request(
    side_effect: SideEffect,
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    deadline: datetime = NOW + timedelta(minutes=1),
) -> SkillRecoveryRequest:
    return SkillRecoveryRequest(
        binding(), side_effect, "stable-key", attempt, max_attempts, deadline, "provider-1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side_effect", [SideEffect.PURE, SideEffect.IDEMPOTENT])
async def test_safe_recovery_types_replay_with_next_attempt(side_effect: SideEffect) -> None:
    invoker = Invoker(SkillResult("UNKNOWN"))
    result = await SkillRecoveryManager(invoker, clock=Clock()).recover(request(side_effect))  # type: ignore[arg-type]
    assert result.action is RecoveryAction.REPLAY
    assert result.next_attempt == 2
    assert invoker.queries == []


@pytest.mark.asyncio
async def test_non_replayable_and_exhausted_or_expired_calls_do_not_replay() -> None:
    manager = SkillRecoveryManager(Invoker(SkillResult("UNKNOWN")), clock=Clock())  # type: ignore[arg-type]
    review = await manager.recover(request(SideEffect.NON_REPLAYABLE))
    exhausted = await manager.recover(request(SideEffect.IDEMPOTENT, attempt=3))
    expired = await manager.recover(
        request(SideEffect.PURE, deadline=NOW - timedelta(seconds=1))
    )
    assert review.action is RecoveryAction.REQUIRE_REVIEW
    assert exhausted.action is RecoveryAction.FAIL
    assert expired.action is RecoveryAction.TIME_OUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "action"),
    [
        (SkillResult("SUCCEEDED", {"value": 1}, "provider-1"), RecoveryAction.COMPLETE),
        (SkillResult("FAILED", {"code": "rejected"}, "provider-1"), RecoveryAction.FAIL),
        (SkillResult("UNKNOWN"), RecoveryAction.REQUIRE_REVIEW),
    ],
)
async def test_queryable_maps_provider_fact_without_blind_replay(
    provider: SkillResult, action: RecoveryAction
) -> None:
    invoker = Invoker(provider)
    result = await SkillRecoveryManager(invoker, clock=Clock()).recover(  # type: ignore[arg-type]
        request(SideEffect.QUERYABLE)
    )
    assert result.action is action
    assert invoker.queries == [("stable-key", "provider-1")]


@pytest.mark.asyncio
async def test_query_failure_requires_review_and_request_is_validated() -> None:
    invoker = Invoker(SkillError("SKILL_RECOVERY_UNKNOWN", "offline"))
    result = await SkillRecoveryManager(invoker, clock=Clock()).recover(  # type: ignore[arg-type]
        request(SideEffect.QUERYABLE)
    )
    assert result.action is RecoveryAction.REQUIRE_REVIEW
    assert "SKILL_RECOVERY_UNKNOWN" in result.reason
    with pytest.raises(ValueError, match="idempotency_key"):
        SkillRecoveryRequest(binding(), SideEffect.PURE, "", 1, 2, NOW)
    with pytest.raises(ValueError, match="max_attempts"):
        SkillRecoveryRequest(binding(), SideEffect.PURE, "key", 1, 4, NOW)
    with pytest.raises(ValueError, match="exceed"):
        SkillRecoveryRequest(binding(), SideEffect.PURE, "key", 3, 2, NOW)
    with pytest.raises(ValueError, match="timezone"):
        SkillRecoveryRequest(binding(), SideEffect.PURE, "key", 1, 2, NOW.replace(tzinfo=None))
