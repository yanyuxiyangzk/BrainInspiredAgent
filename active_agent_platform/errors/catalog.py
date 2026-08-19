"""Stable platform error code catalog."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from active_agent_platform.errors.model import ErrorCategory, ErrorSeverity


class ErrorCode(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    PLAN_SCHEMA_INVALID = "PLAN_SCHEMA_INVALID"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    WORKFLOW_NOT_ALLOWED = "WORKFLOW_NOT_ALLOWED"
    WORKFLOW_GRAPH_INVALID = "WORKFLOW_GRAPH_INVALID"
    WORKFLOW_RECURSION_LIMIT = "WORKFLOW_RECURSION_LIMIT"
    EXPRESSION_NOT_ALLOWED = "EXPRESSION_NOT_ALLOWED"
    PARAMETER_INVALID = "PARAMETER_INVALID"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    BRAIN_MODE_DENIED = "BRAIN_MODE_DENIED"
    DATA_STALE = "DATA_STALE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TASK_STATE_TRANSITION_INVALID = "TASK_STATE_TRANSITION_INVALID"
    TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    SKILL_BINDING_NOT_FOUND = "SKILL_BINDING_NOT_FOUND"
    SKILL_SCHEMA_INCOMPATIBLE = "SKILL_SCHEMA_INCOMPATIBLE"
    SKILL_PERMISSION_DENIED = "SKILL_PERMISSION_DENIED"
    SKILL_UNHEALTHY = "SKILL_UNHEALTHY"
    SKILL_TIMEOUT = "SKILL_TIMEOUT"
    SKILL_CANCEL_FAILED = "SKILL_CANCEL_FAILED"
    SKILL_OUTPUT_INVALID = "SKILL_OUTPUT_INVALID"
    SKILL_RECOVERY_UNKNOWN = "SKILL_RECOVERY_UNKNOWN"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    MEMORY_WRITE_FAILED = "MEMORY_WRITE_FAILED"
    EVENT_QUEUE_FULL = "EVENT_QUEUE_FULL"
    AREA_CRASHED = "AREA_CRASHED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    code: str
    category: ErrorCategory
    retryable: bool | None
    severity: ErrorSeverity
    message: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", self.code) is None:
            raise ValueError("invalid error definition code")
        if not self.message or len(self.message) > 1000:
            raise ValueError("invalid error definition message")


class ErrorCatalog:
    def __init__(self, definitions: Iterable[ErrorDefinition]) -> None:
        by_code: dict[str, ErrorDefinition] = {}
        for definition in definitions:
            if definition.code in by_code:
                raise ValueError(f"duplicate error code: {definition.code}")
            by_code[definition.code] = definition
        self._definitions = MappingProxyType(by_code)

    def get(self, code: str | ErrorCode) -> ErrorDefinition:
        try:
            return self._definitions[str(code)]
        except KeyError as error:
            raise KeyError(f"unknown error code: {code}") from error

    def extend(self, definitions: Iterable[ErrorDefinition]) -> "ErrorCatalog":
        return ErrorCatalog((*self._definitions.values(), *definitions))


def _definition(
    code: ErrorCode,
    category: ErrorCategory,
    retryable: bool | None,
    message: str,
    severity: ErrorSeverity = ErrorSeverity.ERROR,
) -> ErrorDefinition:
    return ErrorDefinition(code.value, category, retryable, severity, message)


CORE_ERROR_CATALOG = ErrorCatalog(
    (
        _definition(ErrorCode.SCHEMA_INVALID, ErrorCategory.VALIDATION, False, "Schema is invalid"),
        _definition(
            ErrorCode.PLAN_SCHEMA_INVALID, ErrorCategory.VALIDATION, False, "Plan is invalid"
        ),
        _definition(ErrorCode.PLAN_EXPIRED, ErrorCategory.POLICY, False, "Plan has expired"),
        _definition(
            ErrorCode.WORKFLOW_NOT_FOUND, ErrorCategory.VALIDATION, False, "Workflow was not found"
        ),
        _definition(
            ErrorCode.WORKFLOW_NOT_ALLOWED, ErrorCategory.POLICY, False, "Workflow is not allowed"
        ),
        _definition(
            ErrorCode.WORKFLOW_GRAPH_INVALID,
            ErrorCategory.VALIDATION,
            False,
            "Workflow graph is invalid",
        ),
        _definition(
            ErrorCode.WORKFLOW_RECURSION_LIMIT,
            ErrorCategory.VALIDATION,
            False,
            "Workflow recursion limit was reached",
        ),
        _definition(
            ErrorCode.EXPRESSION_NOT_ALLOWED,
            ErrorCategory.VALIDATION,
            False,
            "Expression is not allowed",
        ),
        _definition(
            ErrorCode.PARAMETER_INVALID, ErrorCategory.VALIDATION, False, "Parameter is invalid"
        ),
        _definition(ErrorCode.CAPABILITY_DENIED, ErrorCategory.POLICY, False, "Capability denied"),
        _definition(ErrorCode.BRAIN_MODE_DENIED, ErrorCategory.POLICY, False, "Mode denied"),
        _definition(ErrorCode.DATA_STALE, ErrorCategory.POLICY, False, "Input data is stale"),
        _definition(ErrorCode.BUDGET_EXCEEDED, ErrorCategory.POLICY, False, "Budget exceeded"),
        _definition(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            ErrorCategory.CONFLICT,
            False,
            "Idempotency conflict",
        ),
        _definition(
            ErrorCode.TASK_STATE_TRANSITION_INVALID,
            ErrorCategory.CONFLICT,
            False,
            "Task state transition is invalid",
        ),
        _definition(
            ErrorCode.TASK_DEADLINE_EXCEEDED,
            ErrorCategory.TIMEOUT,
            None,
            "Task deadline exceeded",
        ),
        _definition(
            ErrorCode.SKILL_NOT_FOUND, ErrorCategory.VALIDATION, False, "Skill was not found"
        ),
        _definition(
            ErrorCode.SKILL_BINDING_NOT_FOUND,
            ErrorCategory.VALIDATION,
            False,
            "Skill binding was not found",
        ),
        _definition(
            ErrorCode.SKILL_SCHEMA_INCOMPATIBLE,
            ErrorCategory.VALIDATION,
            False,
            "Skill schema is incompatible",
        ),
        _definition(
            ErrorCode.SKILL_PERMISSION_DENIED,
            ErrorCategory.POLICY,
            False,
            "Skill permission denied",
        ),
        _definition(
            ErrorCode.SKILL_UNHEALTHY,
            ErrorCategory.DEPENDENCY,
            True,
            "Skill is unhealthy",
        ),
        _definition(ErrorCode.SKILL_TIMEOUT, ErrorCategory.TIMEOUT, None, "Skill timed out"),
        _definition(
            ErrorCode.SKILL_CANCEL_FAILED,
            ErrorCategory.DEPENDENCY,
            True,
            "Skill cancellation failed",
        ),
        _definition(
            ErrorCode.SKILL_OUTPUT_INVALID,
            ErrorCategory.VALIDATION,
            False,
            "Skill output is invalid",
        ),
        _definition(
            ErrorCode.SKILL_RECOVERY_UNKNOWN,
            ErrorCategory.CONFLICT,
            False,
            "Skill recovery result is unknown",
        ),
        _definition(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            ErrorCategory.DEPENDENCY,
            True,
            "Dependency unavailable",
        ),
        _definition(
            ErrorCode.MEMORY_WRITE_FAILED,
            ErrorCategory.DEPENDENCY,
            True,
            "Memory write failed",
        ),
        _definition(ErrorCode.EVENT_QUEUE_FULL, ErrorCategory.RESOURCE, True, "Event queue is full"),
        _definition(ErrorCode.AREA_CRASHED, ErrorCategory.INTERNAL, True, "Service crashed"),
        _definition(ErrorCode.INTERNAL_ERROR, ErrorCategory.INTERNAL, False, "Internal error"),
    )
)
