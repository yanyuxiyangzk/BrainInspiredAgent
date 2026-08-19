"""Structured logger implementations for production and tests."""

import logging
from dataclasses import dataclass

from active_agent_platform.errors.model import JsonValue
from active_agent_platform.errors.redaction import Redactor
from brain_kernel.ports.logging import LogLevel, LogValue


class StdlibLogger:
    def __init__(self, logger: logging.Logger, redactor: Redactor | None = None) -> None:
        self._logger = logger
        self._redactor = redactor or Redactor()

    def emit(self, level: LogLevel, event: str, **fields: LogValue) -> None:
        self._logger.log(
            int(level),
            self._redactor.redact_text(event),
            extra={"structured_fields": self._redactor.redact_mapping(fields)},
        )


@dataclass(frozen=True, slots=True)
class CapturedLog:
    level: LogLevel
    event: str
    fields: dict[str, JsonValue]


class CapturingLogger:
    def __init__(self, redactor: Redactor | None = None) -> None:
        self.records: list[CapturedLog] = []
        self._redactor = redactor or Redactor()

    def emit(self, level: LogLevel, event: str, **fields: LogValue) -> None:
        self.records.append(
            CapturedLog(
                level=level,
                event=self._redactor.redact_text(event),
                fields=self._redactor.redact_mapping(fields),
            )
        )
