"""Safe construction of catalog-backed Error envelopes."""

from uuid import UUID

from active_agent_platform.errors.catalog import CORE_ERROR_CATALOG, ErrorCatalog, ErrorCode
from active_agent_platform.errors.model import ErrorEnvelope, ErrorSeverity
from active_agent_platform.errors.redaction import Redactor
from brain_kernel.ports import Clock, UuidGenerator


class ErrorFactory:
    def __init__(
        self,
        clock: Clock,
        uuid: UuidGenerator,
        *,
        catalog: ErrorCatalog = CORE_ERROR_CATALOG,
        redactor: Redactor | None = None,
    ) -> None:
        self._clock = clock
        self._uuid = uuid
        self._catalog = catalog
        self._redactor = redactor or Redactor()

    def create(
        self,
        code: str | ErrorCode,
        *,
        source: str,
        correlation_id: UUID,
        retryable: bool | None = None,
        severity: ErrorSeverity | None = None,
        causation_id: UUID | None = None,
        plan_id: UUID | None = None,
        task_id: UUID | None = None,
        node_id: str | None = None,
        details: dict[str, object] | None = None,
        cause: ErrorEnvelope | None = None,
    ) -> ErrorEnvelope:
        definition = self._catalog.get(code)
        resolved_retryable = definition.retryable if retryable is None else retryable
        if resolved_retryable is None:
            raise ValueError(f"retryable must be explicit for {definition.code}")
        return ErrorEnvelope(
            error_id=self._uuid.new(),
            code=definition.code,
            category=definition.category,
            message=self._redactor.redact_text(definition.message),
            retryable=resolved_retryable,
            severity=severity or definition.severity,
            occurred_at=self._clock.now(),
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            plan_id=plan_id,
            task_id=task_id,
            node_id=node_id,
            details=self._redactor.redact_mapping(details or {}),
            cause=cause,
        )

    def from_exception(
        self,
        error: Exception,
        *,
        source: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> ErrorEnvelope:
        return self.create(
            ErrorCode.INTERNAL_ERROR,
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            details={"exception_type": type(error).__name__},
        )
