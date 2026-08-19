from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from active_agent_platform.state import (
    BrainMode,
    MarketHours,
    MarketPhase,
    StateChange,
    StateController,
    TradingCalendar,
    Workload,
)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None

    def set(self, value: datetime) -> None:
        self.value = value


class Calendar(TradingCalendar):
    def __init__(self, holidays: set[date]) -> None:
        self.holidays = holidays

    def is_trading_day(self, value: date) -> bool:
        return super().is_trading_day(value) and value not in self.holidays


def shanghai(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


@pytest.mark.asyncio
async def test_market_phases_follow_calendar_and_boundaries() -> None:
    clock = FakeClock(shanghai(9, 14))
    controller = StateController(clock)
    assert controller.phase_at(clock.now()) is MarketPhase.CLOSED
    clock.set(shanghai(9, 15))
    assert controller.phase_at(clock.now()) is MarketPhase.PRE_OPEN
    clock.set(shanghai(9, 25))
    assert controller.phase_at(clock.now()) is MarketPhase.AUCTION
    clock.set(shanghai(9, 30))
    assert controller.phase_at(clock.now()) is MarketPhase.TRADING
    clock.set(shanghai(15, 30))
    assert controller.phase_at(clock.now()) is MarketPhase.CLOSED

    holiday = StateController(clock, calendar=Calendar({date(2026, 8, 17)}))
    assert holiday.phase_at(clock.now()) is MarketPhase.HOLIDAY


@pytest.mark.asyncio
async def test_ready_refresh_and_review_transitions_emit_once() -> None:
    clock = FakeClock(shanghai(9, 25))
    changes: list[StateChange] = []

    async def sink(change: StateChange) -> None:
        changes.append(change)

    controller = StateController(clock, sink=sink)
    first = await controller.mark_ready(reason="startup")
    duplicate = await controller.refresh(reason="same observation")
    assert first is not None
    assert duplicate is None
    assert first.current == controller.state
    assert first.current.phase is MarketPhase.AUCTION
    assert first.current.brain_mode is BrainMode.NORMAL
    assert first.payload()["event_type"] == "brain.state_changed"
    assert changes == [first]

    clock.set(shanghai(15, 31))
    review = await controller.refresh(reason="market closed")
    assert review is not None
    assert review.current.brain_mode is BrainMode.REVIEW
    assert review.current.phase is MarketPhase.CLOSED
    assert len(changes) == 2


@pytest.mark.asyncio
async def test_workload_and_mode_are_orthogonal_and_safe_is_sticky() -> None:
    clock = FakeClock(shanghai(10))
    controller = StateController(clock)
    await controller.mark_ready()
    busy = await controller.set_workload(Workload.BUSY, reason="task admitted")
    assert busy is not None and busy.current.workload is Workload.BUSY
    safe = await controller.set_mode(BrainMode.SAFE, reason="kill switch")
    assert safe is not None and safe.current.brain_mode is BrainMode.SAFE
    await controller.refresh(reason="clock tick")
    assert controller.state.brain_mode is BrainMode.SAFE
    with pytest.raises(ValueError, match="BOOTING"):
        await controller.set_mode(BrainMode.BOOTING, reason="invalid")


@pytest.mark.asyncio
async def test_shutdown_and_validation() -> None:
    clock = FakeClock(shanghai(10))
    controller = StateController(clock)
    await controller.start()
    await controller.set_mode(BrainMode.SHUTTING_DOWN, reason="shutdown")
    await controller.quiesce()
    await controller.checkpoint()
    await controller.stop()
    assert controller.state.brain_mode is BrainMode.SHUTTING_DOWN
    with pytest.raises(ValueError, match="increasing"):
        MarketHours(pre_open=time(10), auction=time(9, 30))
    with pytest.raises(ValueError):
        MarketHours(timezone="Not/AZone")
    with pytest.raises(ValueError, match="timezone"):
        StateController(FakeClock(datetime(2026, 1, 1)))  # noqa: DTZ001
