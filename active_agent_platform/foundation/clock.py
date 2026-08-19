"""Real and deterministic clock implementations."""

import asyncio
import heapq
import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        await asyncio.sleep(seconds)


class FakeClock:
    """A manually advanced clock; sleepers wake when virtual time reaches their deadline."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._monotonic = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(self._sleepers, (self._monotonic + seconds, self._sequence, future))
        try:
            await future
        finally:
            if future.cancelled():
                self._discard_cancelled_sleepers()

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("advance duration must be non-negative")
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)
        while self._sleepers and self._sleepers[0][0] <= self._monotonic:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)

    def _discard_cancelled_sleepers(self) -> None:
        self._sleepers = [item for item in self._sleepers if not item[2].cancelled()]
        heapq.heapify(self._sleepers)
