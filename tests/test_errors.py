import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from active_agent_platform.errors import (
    CORE_ERROR_CATALOG,
    REDACTED,
    ErrorCatalog,
    ErrorCategory,
    ErrorCode,
    ErrorDefinition,
    ErrorEnvelope,
    ErrorFactory,
    ErrorSeverity,
    Redactor,
)
from active_agent_platform.foundation import CapturingLogger, FakeClock, FakeUuidGenerator
from brain_kernel.ports import LogLevel

START = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
CORRELATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ERROR_IDS = [UUID(int=value) for value in range(1, 20)]
TEST_SECRET = "bia-test-secret-9f3a"
ROOT = Path(__file__).parents[1]


def factory(*, catalog: ErrorCatalog = CORE_ERROR_CATALOG) -> ErrorFactory:
    return ErrorFactory(
        FakeClock(START),
        FakeUuidGenerator(ERROR_IDS),
        catalog=catalog,
    )


def test_error_factory_output_matches_error_1_schema() -> None:
    error = factory().create(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        source="adapter",
        correlation_id=CORRELATION_ID,
        causation_id=UUID(int=100),
        plan_id=UUID(int=101),
        task_id=UUID(int=102),
        node_id="fetch",
        details={"attempt": 2, "endpoint": "service.local"},
    )
    document = error.to_dict()
    schema = json.loads(
        (ROOT / "schemas/error/error-1.0.schema.json").read_text(encoding="utf-8")
    )

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    assert document["schema_version"] == "1.0"
    assert document["occurred_at"] == "2026-08-17T08:00:00Z"
    assert document["category"] == "DEPENDENCY"
    assert document["retryable"] is True


def test_every_declared_error_code_has_a_catalog_definition() -> None:
    definitions = [CORE_ERROR_CATALOG.get(code) for code in ErrorCode]
    assert len(definitions) == len(ErrorCode)
    assert {definition.code for definition in definitions} == {code.value for code in ErrorCode}


def test_variable_retryability_must_be_resolved_per_error() -> None:
    errors = factory()
    with pytest.raises(ValueError, match="retryable must be explicit"):
        errors.create(
            ErrorCode.SKILL_TIMEOUT,
            source="runtime",
            correlation_id=CORRELATION_ID,
        )

    timeout = errors.create(
        ErrorCode.SKILL_TIMEOUT,
        source="runtime",
        correlation_id=CORRELATION_ID,
        retryable=True,
    )
    assert timeout.retryable is True


def test_catalog_rejects_unknown_duplicate_and_invalid_definitions() -> None:
    with pytest.raises(KeyError, match="unknown error code"):
        CORE_ERROR_CATALOG.get("APP_UNKNOWN")
    definition = ErrorDefinition(
        "APP_FAILURE",
        ErrorCategory.INTERNAL,
        False,
        ErrorSeverity.ERROR,
        "Application failure",
    )
    with pytest.raises(ValueError, match="duplicate"):
        ErrorCatalog([definition, definition])
    with pytest.raises(ValueError, match="definition code"):
        ErrorDefinition(
            "bad-code",
            ErrorCategory.INTERNAL,
            False,
            ErrorSeverity.ERROR,
            "Bad code",
        )
    with pytest.raises(ValueError, match="definition message"):
        ErrorDefinition(
            "APP_EMPTY",
            ErrorCategory.INTERNAL,
            False,
            ErrorSeverity.ERROR,
            "",
        )


def test_catalog_can_be_extended_without_mutating_core_catalog() -> None:
    custom = ErrorDefinition(
        "APP_REJECTED",
        ErrorCategory.POLICY,
        False,
        ErrorSeverity.WARNING,
        "Application request rejected",
    )
    extended = CORE_ERROR_CATALOG.extend([custom])
    error = factory(catalog=extended).create(
        "APP_REJECTED", source="app", correlation_id=CORRELATION_ID
    )

    assert error.category is ErrorCategory.POLICY
    assert error.severity is ErrorSeverity.WARNING
    with pytest.raises(KeyError):
        CORE_ERROR_CATALOG.get("APP_REJECTED")


def test_error_details_are_deeply_immutable_and_serialization_is_detached() -> None:
    source_details = {"nested": {"values": [1, 2]}}
    error = factory().create(
        ErrorCode.INTERNAL_ERROR,
        source="runtime",
        correlation_id=CORRELATION_ID,
        details=source_details,
    )
    source_details["nested"] = {"values": [99]}
    serialized = error.to_dict()
    assert serialized["details"] == {"nested": {"values": [1, 2]}}

    with pytest.raises(TypeError):
        error.details["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        error.code = "INTERNAL_ERROR"  # type: ignore[misc]


def test_cause_chain_serializes_to_depth_five_and_rejects_six() -> None:
    errors = factory()
    cause: ErrorEnvelope | None = None
    for _ in range(5):
        cause = errors.create(
            ErrorCode.INTERNAL_ERROR,
            source="runtime",
            correlation_id=CORRELATION_ID,
            cause=cause,
        )
    assert cause is not None
    assert cause.cause_depth == 5
    assert _serialized_depth(cause.to_dict()) == 5

    with pytest.raises(ValueError, match="depth"):
        errors.create(
            ErrorCode.INTERNAL_ERROR,
            source="runtime",
            correlation_id=CORRELATION_ID,
            cause=cause,
        )


def test_error_model_rejects_invalid_contract_fields() -> None:
    base = {
        "error_id": UUID(int=1),
        "code": "INTERNAL_ERROR",
        "category": ErrorCategory.INTERNAL,
        "message": "Internal error",
        "retryable": False,
        "severity": ErrorSeverity.ERROR,
        "occurred_at": START,
        "source": "runtime",
        "correlation_id": CORRELATION_ID,
    }
    for field, value, message in (
        ("code", "bad", "error code"),
        ("message", "", "message length"),
        ("source", "", "source length"),
        ("node_id", "n" * 129, "node_id"),
        ("occurred_at", datetime.fromisoformat("2026-08-17T08:00:00"), "timezone-aware"),
        ("details", {f"key_{index}": index for index in range(33)}, "32 properties"),
    ):
        values = {**base, field: value}
        with pytest.raises(ValueError, match=message):
            ErrorEnvelope(**values)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="JSON-compatible"):
        ErrorEnvelope(**base, details={"unsupported": object()})  # type: ignore[arg-type]


def test_redactor_removes_nested_credentials_paths_sql_and_unknown_objects() -> None:
    redactor = Redactor()
    result = redactor.redact_mapping(
        {
            "api_key": TEST_SECRET,
            "nested": {
                "password": TEST_SECRET,
                "header": f"Bearer {TEST_SECRET}",
                "location": "/srv/private/config.json",
            },
            "query": "SELECT password FROM users",
            "unknown": object(),
        }
    )
    rendered = json.dumps(result)

    assert TEST_SECRET not in rendered
    assert result["api_key"] == REDACTED
    assert result["query"] == REDACTED
    assert "[REDACTED_PATH]" in rendered
    assert "[REDACTED_TYPE:object]" in rendered


def test_redactor_limits_depth_and_string_size() -> None:
    nested: dict[str, object] = {}
    current = nested
    for _ in range(12):
        child: dict[str, object] = {}
        current["child"] = child
        current = child
    result = Redactor().redact_mapping({"nested": nested, "long": "x" * 1100})
    rendered = json.dumps(result)
    assert "[REDACTED_DEPTH]" in rendered
    assert "[TRUNCATED]" in rendered


def test_exception_mapping_never_exposes_exception_message() -> None:
    error = factory().from_exception(
        RuntimeError(f"password={TEST_SECRET} at /private/config"),
        source="runtime",
        correlation_id=CORRELATION_ID,
    )
    rendered = json.dumps(error.to_dict())

    assert TEST_SECRET not in rendered
    assert "/private/config" not in rendered
    assert error.details == {"exception_type": "RuntimeError"}


def test_all_structured_loggers_redact_secret_fields_and_event_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capturing = CapturingLogger()
    capturing.emit(
        LogLevel.ERROR,
        f"request failed token={TEST_SECRET}",
        secret=TEST_SECRET,
        detail=f"Bearer {TEST_SECRET}",
    )
    captured_rendered = repr(capturing.records)
    assert TEST_SECRET not in captured_rendered
    assert captured_rendered.count(REDACTED) >= 3

    import logging

    from active_agent_platform.foundation import StdlibLogger

    logger = logging.getLogger("bia.redaction.tests")
    with caplog.at_level(logging.ERROR, logger="bia.redaction.tests"):
        StdlibLogger(logger).emit(
            LogLevel.ERROR,
            "request failed",
            token=TEST_SECRET,
        )
    assert TEST_SECRET not in repr(caplog.records[-1].__dict__)
    assert caplog.records[-1].structured_fields["token"] == REDACTED


def _serialized_depth(document: dict[str, object]) -> int:
    depth = 1
    current = document
    while isinstance(current.get("cause"), dict):
        depth += 1
        current = current["cause"]  # type: ignore[assignment]
    return depth
