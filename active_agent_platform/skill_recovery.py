"""Deterministic D06 recovery matrix for interrupted Skill invocations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from active_agent_platform.skills import SideEffect, SkillBinding, SkillError, SkillInvoker
from brain_kernel.ports import Clock


class RecoveryAction(StrEnum):
    REPLAY = "REPLAY"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    TIME_OUT = "TIME_OUT"


@dataclass(frozen=True, slots=True)
class SkillRecoveryRequest:
    binding: SkillBinding
    side_effect: SideEffect
    idempotency_key: str
    attempt: int
    max_attempts: int
    deadline: datetime
    provider_operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("idempotency_key must contain 1 to 255 characters")
        if self.attempt < 1 or not 1 <= self.max_attempts <= 3:
            raise ValueError("attempts must be positive and max_attempts cannot exceed 3")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt cannot exceed max_attempts")
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SkillRecoveryResult:
    action: RecoveryAction
    reason: str
    next_attempt: int | None = None
    output: object | None = None
    provider_operation_id: str | None = None


class SkillRecoveryManager:
    """Classify interrupted calls without changing their pinned binding or idempotency key."""

    def __init__(self, invoker: SkillInvoker, *, clock: Clock) -> None:
        self._invoker = invoker
        self._clock = clock

    async def recover(self, request: SkillRecoveryRequest) -> SkillRecoveryResult:
        if self._clock.now().astimezone(UTC) >= request.deadline.astimezone(UTC):
            return SkillRecoveryResult(RecoveryAction.TIME_OUT, "task deadline expired")
        if request.side_effect is SideEffect.NON_REPLAYABLE:
            return SkillRecoveryResult(
                RecoveryAction.REQUIRE_REVIEW,
                "non-replayable invocation has an unknown outcome",
            )
        if request.side_effect is SideEffect.QUERYABLE:
            return await self._recover_queryable(request)
        if request.attempt >= request.max_attempts:
            return SkillRecoveryResult(RecoveryAction.FAIL, "recovery attempts exhausted")
        reason = (
            "pure invocation is safe to replay"
            if request.side_effect is SideEffect.PURE
            else "idempotent invocation must replay with the original key"
        )
        return SkillRecoveryResult(
            RecoveryAction.REPLAY,
            reason,
            next_attempt=request.attempt + 1,
        )

    async def _recover_queryable(
        self, request: SkillRecoveryRequest
    ) -> SkillRecoveryResult:
        try:
            result = await self._invoker.query_result(
                request.binding,
                request.idempotency_key,
                request.provider_operation_id,
            )
        except SkillError as error:
            return SkillRecoveryResult(
                RecoveryAction.REQUIRE_REVIEW,
                f"recovery query failed: {error.code}",
                provider_operation_id=request.provider_operation_id,
            )
        if result.status == "SUCCEEDED":
            return SkillRecoveryResult(
                RecoveryAction.COMPLETE,
                "provider confirms success",
                output=result.output,
                provider_operation_id=result.provider_operation_id,
            )
        if result.status == "FAILED":
            return SkillRecoveryResult(
                RecoveryAction.FAIL,
                "provider confirms failure",
                output=result.output,
                provider_operation_id=result.provider_operation_id,
            )
        return SkillRecoveryResult(
            RecoveryAction.REQUIRE_REVIEW,
            f"provider result is {result.status}",
            provider_operation_id=result.provider_operation_id or request.provider_operation_id,
        )
