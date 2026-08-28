"""Validated, immutable platform settings."""

import os
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass

from brain_kernel.ports import LogLevel


@dataclass(frozen=True, slots=True)
class Settings:
    service_name: str = "bia-agent"
    environment: str = "development"
    log_level: LogLevel = LogLevel.INFO
    shutdown_timeout_seconds: float = 30.0
    model_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    model_provider: str = "openai-compatible"

    def __post_init__(self) -> None:
        if not self.service_name.strip():
            raise ValueError("service_name must not be empty")
        if not self.environment.strip():
            raise ValueError("environment must not be empty")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        if env is None:
            loaded: dict[str, str] = {}
            # Load the nearest project .env without overriding exported variables.
            for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
                if candidate.is_file():
                    for line in candidate.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key, value = key.strip(), value.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            loaded[key] = value
            loaded.update(os.environ)
            values = loaded
        else:
            values = env
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
            model_url=values.get("BIA_MODEL_URL", ""),
            model_name=values.get("BIA_MODEL_NAME", ""),
            model_api_key=values.get("BIA_MODEL_API_KEY", ""),
            model_provider=values.get("BIA_MODEL_PROVIDER", "openai-compatible"),
        )
