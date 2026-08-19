"""Minimal structured logging abstraction."""

from enum import IntEnum
from typing import Protocol

LogValue = str | int | float | bool | None


class LogLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


class StructuredLogger(Protocol):
    """Emit named events with machine-readable scalar fields."""

    def emit(self, level: LogLevel, event: str, **fields: LogValue) -> None:
        """Emit one structured event."""
