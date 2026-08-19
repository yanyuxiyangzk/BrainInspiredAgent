"""Recursive redaction shared by errors, logs and future traces."""

import re
from collections.abc import Mapping, Sequence

from active_agent_platform.errors.model import JsonValue

REDACTED = "[REDACTED]"
_MAX_DEPTH = 10
_MAX_STRING_LENGTH = 1000
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "apikey",
    "accesskey",
    "privatekey",
    "cookie",
    "prompt",
    "sql",
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_UNIX_PATH_PATTERN = re.compile(r"(?<![\w.])/(?:[^\s/]+/)*[^\s/]+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:\\(?:[^\s\\]+\\)*[^\s\\]+")
_UNSAFE_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(traceback|\bselect\b.+\bfrom\b|\binsert\s+into\b|\bupdate\b.+\bset\b|\bdelete\s+from\b)"
)


class Redactor:
    def redact_text(self, value: str) -> str:
        if _UNSAFE_DIAGNOSTIC_PATTERN.search(value):
            return REDACTED
        value = _CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", value)
        value = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", value)
        value = _WINDOWS_PATH_PATTERN.sub("[REDACTED_PATH]", value)
        value = _UNIX_PATH_PATTERN.sub("[REDACTED_PATH]", value)
        if len(value) > _MAX_STRING_LENGTH:
            return f"{value[:_MAX_STRING_LENGTH]}[TRUNCATED]"
        return value

    def redact_mapping(self, values: Mapping[str, object]) -> dict[str, JsonValue]:
        return {
            str(key): REDACTED if self._is_sensitive_key(str(key)) else self._redact(value, 1)
            for key, value in values.items()
        }

    def _redact(self, value: object, depth: int) -> JsonValue:
        if depth >= _MAX_DEPTH:
            return "[REDACTED_DEPTH]"
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {
                str(key): REDACTED if self._is_sensitive_key(str(key)) else self._redact(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [self._redact(item, depth + 1) for item in value]
        return f"[REDACTED_TYPE:{type(value).__name__}]"

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        return any(term in normalized for term in _SENSITIVE_KEYS)
