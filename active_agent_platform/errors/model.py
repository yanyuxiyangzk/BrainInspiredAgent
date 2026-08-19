"""Immutable code representation of the Error 1.0 contract."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class ErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    POLICY = "POLICY"
    DEPENDENCY = "DEPENDENCY"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    RESOURCE = "RESOURCE"
    INTERNAL = "INTERNAL"


class ErrorSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    error_id: UUID
    code: str
    category: ErrorCategory
    message: str
    retryable: bool
    severity: ErrorSeverity
    occurred_at: datetime
    source: str
    correlation_id: UUID
    causation_id: UUID | None = None
    plan_id: UUID | None = None
    task_id: UUID | None = None
    node_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)
    cause: "ErrorEnvelope | None" = None
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        if not _CODE_PATTERN.fullmatch(self.code):
            raise ValueError("error code must match the Error 1.0 contract")
        if not self.message or len(self.message) > 1000:
            raise ValueError("message length must be between 1 and 1000")
        if not self.source or len(self.source) > 100:
            raise ValueError("source length must be between 1 and 100")
        if self.node_id is not None and len(self.node_id) > 128:
            raise ValueError("node_id must not exceed 128 characters")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if len(self.details) > 32:
            raise ValueError("details must not contain more than 32 properties")
        if self.cause_depth > 5:
            raise ValueError("error cause depth must not exceed 5")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    @property
    def cause_depth(self) -> int:
        depth = 1
        current = self.cause
        seen = {id(self)}
        while current is not None:
            if id(current) in seen:
                raise ValueError("error cause chain must not contain a cycle")
            seen.add(id(current))
            depth += 1
            current = current.cause
        return depth

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "error_id": str(self.error_id),
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "severity": self.severity.value,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "source": self.source,
            "correlation_id": str(self.correlation_id),
        }
        optional_ids = {
            "causation_id": self.causation_id,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
        }
        result.update({key: str(value) for key, value in optional_ids.items() if value is not None})
        if self.node_id is not None:
            result["node_id"] = self.node_id
        if self.details:
            result["details"] = _thaw(self.details)
        if self.cause is not None:
            result["cause"] = self.cause.to_dict()
        return result


def _freeze_mapping(values: Mapping[str, object]) -> Mapping[str, FrozenJsonValue]:
    return MappingProxyType({key: _freeze(value) for key, value in values.items()})


def _freeze(value: object) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("details must contain JSON-compatible values")


def _thaw(value: object) -> JsonValue:
    if isinstance(value, MappingProxyType):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("unsupported frozen JSON value")
