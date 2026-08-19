"""Domain-neutral three-dimensional brain state controller."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain_kernel.ports import Clock


class MarketPhase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    AUCTION = "AUCTION"
    TRADING = "TRADING"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"


class Workload(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"


class BrainMode(StrEnum):
    BOOTING = "BOOTING"
    NORMAL = "NORMAL"
    REVIEW = "REVIEW"
    DEGRADED = "DEGRADED"
    SAFE = "SAFE"
    SHUTTING_DOWN = "SHUTTING_DOWN"


@dataclass(frozen=True, slots=True)
class BrainState:
    phase: MarketPhase
    workload: Workload
    brain_mode: BrainMode
    observed_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "workload": self.workload.value,
            "brain_mode": self.brain_mode.value,
            "observed_at": self.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class StateChange:
    previous: BrainState
    current: BrainState
    reason: str

    def payload(self) -> dict[str, object]:
        return {
            "event_type": "brain.state_changed",
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
            "reason": self.reason,
        }


class TradingCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


@dataclass(frozen=True, slots=True)
class MarketHours:
    timezone: str = "Asia/Shanghai"
    pre_open: time = time(9, 15)
    auction: time = time(9, 25)
    trading: time = time(9, 30)
    close: time = time(15, 30)

    def __post_init__(self) -> None:
        if not self.pre_open < self.auction < self.trading < self.close:
            raise ValueError("session hours must be strictly increasing")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA zone") from error


StateSink = Callable[[StateChange], Awaitable[None] | None]


class StateController:
    """Compute and publish state transitions without deciding business tasks."""

    name = "state_controller"

    def __init__(
        self,
        clock: Clock,
        *,
        calendar: TradingCalendar | None = None,
        hours: MarketHours | None = None,
        sink: StateSink | None = None,
    ) -> None:
        self._clock = clock
        self._calendar = calendar or TradingCalendar()
        self._hours = hours or MarketHours()
        self._sink = sink
        now = clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock.now() must be timezone-aware")
        now = now.astimezone(UTC)
        self._state = BrainState(MarketPhase.CLOSED, Workload.IDLE, BrainMode.BOOTING, now)
        self._accepting = False

    @property
    def state(self) -> BrainState:
        return self._state

    async def start(self) -> None:
        self._accepting = True

    async def serve(self) -> None:
        # Time-driven transitions belong to Scheduler; this service is event-driven.
        await self._wait_until_stopped()

    async def quiesce(self) -> None:
        self._accepting = False

    async def checkpoint(self) -> None:
        return None

    async def stop(self) -> None:
        self._accepting = False

    async def refresh(self, *, reason: str = "clock refresh") -> StateChange | None:
        phase = self.phase_at(self._clock.now())
        mode = self._state.brain_mode
        if mode is BrainMode.NORMAL and phase in {MarketPhase.CLOSED, MarketPhase.HOLIDAY}:
            mode = BrainMode.REVIEW
        elif mode is BrainMode.REVIEW and phase not in {MarketPhase.CLOSED, MarketPhase.HOLIDAY}:
            mode = BrainMode.NORMAL
        return await self._replace(phase=phase, brain_mode=mode, reason=reason)

    async def mark_ready(self, *, reason: str = "dependencies ready") -> StateChange | None:
        if self._state.brain_mode is not BrainMode.BOOTING:
            return None
        phase = self.phase_at(self._clock.now())
        mode = BrainMode.REVIEW if phase in {MarketPhase.CLOSED, MarketPhase.HOLIDAY} else BrainMode.NORMAL
        return await self._replace(phase=phase, brain_mode=mode, reason=reason)

    async def set_workload(self, workload: Workload, *, reason: str) -> StateChange | None:
        return await self._replace(workload=workload, reason=reason)

    async def set_mode(self, mode: BrainMode, *, reason: str) -> StateChange | None:
        if mode is BrainMode.BOOTING and self._state.brain_mode is not BrainMode.BOOTING:
            raise ValueError("BOOTING is only valid before readiness")
        return await self._replace(brain_mode=mode, reason=reason)

    def phase_at(self, instant: datetime) -> MarketPhase:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        local = instant.astimezone(ZoneInfo(self._hours.timezone))
        if not self._calendar.is_trading_day(local.date()):
            return MarketPhase.HOLIDAY
        current = local.time().replace(tzinfo=None)
        if current < self._hours.pre_open:
            return MarketPhase.CLOSED
        if current < self._hours.auction:
            return MarketPhase.PRE_OPEN
        if current < self._hours.trading:
            return MarketPhase.AUCTION
        if current < self._hours.close:
            return MarketPhase.TRADING
        return MarketPhase.CLOSED

    async def _replace(
        self,
        *,
        reason: str,
        phase: MarketPhase | None = None,
        workload: Workload | None = None,
        brain_mode: BrainMode | None = None,
    ) -> StateChange | None:
        now = self._clock.now().astimezone(UTC)
        current = BrainState(
            phase or self._state.phase,
            workload or self._state.workload,
            brain_mode or self._state.brain_mode,
            now,
        )
        previous = self._state
        if (
            previous.phase is current.phase
            and previous.workload is current.workload
            and previous.brain_mode is current.brain_mode
        ):
            return None
        self._state = current
        change = StateChange(previous, current, reason)
        if self._sink is not None:
            result = self._sink(change)
            if result is not None:
                await result
        return change

    async def _wait_until_stopped(self) -> None:
        # Kept as a cooperative service hook; LoopEngine owns the actual heartbeat.
        while self._accepting:
            await self._clock.sleep(3600)
