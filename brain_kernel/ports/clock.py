"""Time abstraction for deterministic scheduling and replay."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Provide wall, monotonic and asynchronous time through one injectable port."""

    def now(self) -> datetime:
        """Return an aware UTC wall-clock timestamp."""

    def monotonic(self) -> float:
        """Return monotonic seconds for durations and deadlines."""

    async def sleep(self, seconds: float) -> None:
        """Wait for the requested duration without blocking the event loop."""
