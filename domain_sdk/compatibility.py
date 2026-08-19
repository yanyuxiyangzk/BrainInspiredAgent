"""Compatibility gates for public APIs, schemas and domain plugins."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

PUBLIC_API_VERSION = "1.0"
SUPPORTED_SCHEMA_MAJOR = 1
SUPPORTED_PLUGIN_API_MAJOR = 1
PUBLIC_API_SYMBOLS = (
    "CapabilityContract", "CompositionRoot", "DomainPlugin", "DomainSkillBridge",
    "PluginContribution", "RuntimeBuilder", "SkillManifest", "WorkflowRegistration",
)


class CompatibilityError(ValueError):
    """Raised before startup when a contract cannot be safely consumed."""


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    api_version: str
    schema_major: int
    plugin_api_major: int
    symbols: tuple[str, ...]


def public_api_manifest() -> CompatibilityReport:
    return CompatibilityReport(
        PUBLIC_API_VERSION, SUPPORTED_SCHEMA_MAJOR, SUPPORTED_PLUGIN_API_MAJOR,
        PUBLIC_API_SYMBOLS,
    )


def require_version(version: str, *, kind: str, supported_major: int) -> None:
    match = re.fullmatch(r"([1-9][0-9]*)\.[0-9]+(?:\.[0-9]+)?", version)
    if match is None or int(match.group(1)) != supported_major:
        raise CompatibilityError(f"{kind} version {version!r} is incompatible with supported major {supported_major}")


def assert_schema_backward_compatible(old: Mapping[str, object], new: Mapping[str, object]) -> None:
    """Ensure a new object schema still accepts every document accepted by old."""
    _compare(old, new, path="$")


def _compare(old: Mapping[str, object], new: Mapping[str, object], *, path: str) -> None:
    if old.get("type") != new.get("type"):
        raise CompatibilityError(f"schema type narrowed at {path}")
    if old.get("const") is not None and old.get("const") != new.get("const"):
        raise CompatibilityError(f"schema const changed at {path}")
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list) and not set(old_enum) <= set(new_enum):
        raise CompatibilityError(f"schema enum narrowed at {path}")
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    if isinstance(old_props, Mapping) and isinstance(new_props, Mapping):
        for name, value in old_props.items():
            if name not in new_props and new.get("additionalProperties", True) is False:
                raise CompatibilityError(f"schema removed property at {path}.{name}")
            if name in new_props and isinstance(value, Mapping) and isinstance(new_props[name], Mapping):
                _compare(value, new_props[name], path=f"{path}.{name}")
    old_required = old.get("required", [])
    new_required = new.get("required", [])
    if isinstance(old_required, list) and isinstance(new_required, list) and not set(new_required) <= set(old_required):
        raise CompatibilityError(f"schema added required fields at {path}")
    if old.get("additionalProperties", True) is not False and new.get("additionalProperties", True) is False:
        raise CompatibilityError(f"schema disallows previously accepted properties at {path}")
