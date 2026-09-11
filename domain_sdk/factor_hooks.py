"""Loop event hooks and summaries for the factor discovery profile (L-009).

架构 §6：Hooks 是 Loop 事件订阅者，不是隐藏控制流——``iteration.completed``
输出摘要、``iteration.failed`` 记录失败、``checkpoint.committed`` 校验备份、
``factor.accepted`` 更新覆盖、``profile.stalled`` 请求调整。总线按幂等键去重：
崩溃后重放同键事件对订阅者可见次数为一；订阅者异常被记录且不阻塞其余订阅者，
更不回传主流程。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

_MAX_REMEMBERED_KEYS = 4096


class FactorHookEvent(StrEnum):
    ITERATION_COMPLETED = "iteration.completed"
    ITERATION_FAILED = "iteration.failed"
    CHECKPOINT_COMMITTED = "checkpoint.committed"
    FACTOR_ACCEPTED = "factor.accepted"
    PROFILE_STALLED = "profile.stalled"


@dataclass(frozen=True, slots=True)
class FactorHookPayload:
    """一次 Loop 事件的可校验上下文。"""

    event: FactorHookEvent
    profile_id: str
    profile_version: str
    iteration: int
    digest: str
    details: Mapping[str, object]
    idempotency_key: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event.value,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "iteration": self.iteration,
            "digest": self.digest,
            "details": dict(self.details),
            "idempotency_key": self.idempotency_key,
        }


class FactorHook(Protocol):
    name: str

    async def handle(self, payload: FactorHookPayload) -> None: ...


class FactorHookBus:
    """按幂等键去重的事件总线：同键重放是空操作，订阅者异常不阻塞他人。"""

    def __init__(self, profile_id: str, profile_version: str) -> None:
        self._profile_id = profile_id
        self._profile_version = profile_version
        self._hooks: dict[FactorHookEvent, list[FactorHook]] = {}
        self._delivered: deque[str] = deque(maxlen=_MAX_REMEMBERED_KEYS)
        self._delivered_set: set[str] = set()
        self.errors: list[tuple[str, str]] = []

    def register(self, event: FactorHookEvent, hook: FactorHook) -> None:
        subscribers = self._hooks.setdefault(event, [])
        if any(existing.name == hook.name for existing in subscribers):
            raise ValueError(f"hook {hook.name!r} is already registered for {event.value}")
        subscribers.append(hook)

    async def emit(
        self,
        event: FactorHookEvent,
        *,
        iteration: int,
        digest: str,
        details: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        key = idempotency_key or (
            f"{event.value}:{self._profile_id}:{self._profile_version}:{iteration}:{digest}"
        )
        if key in self._delivered_set:
            return 0
        self._delivered.append(key)
        self._delivered_set.add(key)
        payload = FactorHookPayload(
            event=event,
            profile_id=self._profile_id,
            profile_version=self._profile_version,
            iteration=iteration,
            digest=digest,
            details=dict(details or {}),
            idempotency_key=key,
        )
        delivered = 0
        for hook in self._hooks.get(event, ()):
            try:
                await hook.handle(payload)
                delivered += 1
            except Exception as error:  # noqa: BLE001 - 订阅者故障不阻塞 Loop
                self.errors.append((hook.name, str(error)))
        return delivered


class FactorIterationSummaryCollector:
    """``iteration.completed``/``iteration.failed`` 的运行摘要。

    ``records`` 只保留最近窗口的明细；completed/failed 与候选、入库总数是
    独立累计计数器，不随窗口滑动丢失。
    """

    name = "iteration-summary"

    def __init__(self, *, capacity: int = 128) -> None:
        self._records: deque[dict[str, object]] = deque(maxlen=capacity)
        self._completed = 0
        self._failed = 0
        self._candidates_total = 0
        self._accepted_total = 0

    async def handle(self, payload: FactorHookPayload) -> None:
        candidates = int(cast(int, payload.details.get("candidates", 0)))
        accepted = int(cast(int, payload.details.get("accepted", 0)))
        if payload.event is FactorHookEvent.ITERATION_COMPLETED:
            self._completed += 1
        else:
            self._failed += 1
        self._candidates_total += candidates
        self._accepted_total += accepted
        self._records.append(
            {
                "iteration": payload.iteration,
                "outcome": payload.event.value,
                "candidates": candidates,
                "accepted": accepted,
                "failure_modes": tuple(
                    cast(Iterable[str], payload.details.get("failure_modes", ()))
                ),
            }
        )

    def summary(self) -> dict[str, object]:
        return {
            "iterations": self._completed + self._failed,
            "completed": self._completed,
            "failed": self._failed,
            "candidates_total": self._candidates_total,
            "accepted_total": self._accepted_total,
            "last_iteration": max(
                (int(cast(int, r["iteration"])) for r in self._records), default=0
            ),
        }


class FactorFailureModeCollector:
    """聚合 ``iteration.failed`` 事件里的失败模式计数。"""

    name = "failure-modes"

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def handle(self, payload: FactorHookPayload) -> None:
        modes = cast(Iterable[str], payload.details.get("failure_modes", ()))
        for mode in modes:
            self._counts[str(mode)] = self._counts.get(str(mode), 0) + 1

    def summary(self) -> dict[str, int]:
        return dict(self._counts)


class FactorCoverageCollector:
    """``factor.accepted`` 的覆盖统计：总量与按生成策略分布。"""

    name = "factor-coverage"

    def __init__(self) -> None:
        self._total = 0
        self._by_strategy: dict[str, int] = {}

    async def handle(self, payload: FactorHookPayload) -> None:
        self._total += 1
        strategy = str(payload.details.get("strategy", "unknown"))
        self._by_strategy[strategy] = self._by_strategy.get(strategy, 0) + 1

    def summary(self) -> dict[str, int]:
        return {"total": self._total, **self._by_strategy}
