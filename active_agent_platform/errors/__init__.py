"""Versioned error envelopes, catalog and redaction utilities."""

from active_agent_platform.errors.catalog import (
    CORE_ERROR_CATALOG,
    ErrorCatalog,
    ErrorCode,
    ErrorDefinition,
)
from active_agent_platform.errors.factory import ErrorFactory
from active_agent_platform.errors.model import ErrorCategory, ErrorEnvelope, ErrorSeverity
from active_agent_platform.errors.redaction import REDACTED, Redactor

__all__ = [
    "CORE_ERROR_CATALOG",
    "REDACTED",
    "ErrorCatalog",
    "ErrorCategory",
    "ErrorCode",
    "ErrorDefinition",
    "ErrorEnvelope",
    "ErrorFactory",
    "ErrorSeverity",
    "Redactor",
]
