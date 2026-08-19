"""Explicit runtime dependency bundle used at composition boundaries."""

from dataclasses import dataclass

from active_agent_platform.foundation.settings import Settings
from brain_kernel.ports import Clock, StructuredLogger, UuidGenerator


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    settings: Settings
    clock: Clock
    uuid: UuidGenerator
    logger: StructuredLogger
