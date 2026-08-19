"""Injectable platform foundations shared by runtime components."""

from active_agent_platform.foundation.clock import FakeClock, SystemClock
from active_agent_platform.foundation.container import RuntimeDependencies
from active_agent_platform.foundation.identity import FakeUuidGenerator, Uuid7Generator
from active_agent_platform.foundation.logging import CapturedLog, CapturingLogger, StdlibLogger
from active_agent_platform.foundation.settings import Settings

__all__ = [
    "CapturedLog",
    "CapturingLogger",
    "FakeClock",
    "FakeUuidGenerator",
    "RuntimeDependencies",
    "Settings",
    "StdlibLogger",
    "SystemClock",
    "Uuid7Generator",
]
