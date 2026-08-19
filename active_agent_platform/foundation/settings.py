"""Validated, immutable platform settings."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from brain_kernel.ports import LogLevel


@dataclass(frozen=True, slots=True)
class Settings:
    service_name: str = "bia-agent"
    environment: str = "development"
    log_level: LogLevel = LogLevel.INFO
    shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.service_name.strip():
            raise ValueError("service_name must not be empty")
        if not self.environment.strip():
            raise ValueError("environment must not be empty")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        level_name = values.get("BIA_LOG_LEVEL", "INFO").upper()
        try:
            level = LogLevel[level_name]
        except KeyError as error:
            raise ValueError(f"invalid BIA_LOG_LEVEL: {level_name}") from error
        try:
            timeout = float(values.get("BIA_SHUTDOWN_TIMEOUT_SECONDS", "30"))
        except ValueError as error:
            raise ValueError("BIA_SHUTDOWN_TIMEOUT_SECONDS must be numeric") from error
        return cls(
            service_name=values.get("BIA_SERVICE_NAME", "bia-agent"),
            environment=values.get("BIA_ENVIRONMENT", "development"),
            log_level=level,
            shutdown_timeout_seconds=timeout,
        )
