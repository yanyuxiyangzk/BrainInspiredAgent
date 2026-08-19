"""Versioned DNA envelopes that drive the existing Workflow runtime."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from active_agent_platform.workflow import WorkflowValidation, WorkflowValidator


class DnaStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"


class DnaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DnaParent:
    dna_id: str
    version: str
    content_digest: str

    def to_document(self) -> dict[str, str]:
        return {"dna_id": self.dna_id, "version": self.version,
                "content_digest": self.content_digest}


@dataclass(frozen=True, slots=True)
class DnaDefinition:
    dna_id: str
    version: str
    status: DnaStatus
    workflow: Mapping[str, object]
    content_digest: str
    envelope_digest: str
    workflow_validation: WorkflowValidation
    parent_dna: tuple[DnaParent, ...] = ()
    generator: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def digest(self) -> str:
        """Compatibility alias; execution identity is the content digest."""
        return self.content_digest

    @classmethod
    def from_workflow(
        cls, workflow: Mapping[str, object], *, dna_id: str | None = None,
        version: str | None = None, status: DnaStatus = DnaStatus.CANDIDATE,
        parent_dna: Iterable[DnaParent] = (), generator: Mapping[str, str] | None = None,
        validator: WorkflowValidator | None = None,
    ) -> DnaDefinition:
        if not isinstance(workflow.get("workflow_id"), str) or not isinstance(workflow.get("version"), str):
            raise DnaError("DNA workflow must have workflow_id and version")
        identity = str(workflow["workflow_id"]) if dna_id is None else dna_id
        dna_version = str(workflow["version"]) if version is None else version
        if re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", identity) is None:
            raise DnaError("dna_id is invalid")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", dna_version) is None:
            raise DnaError("DNA version is invalid")
        validation = (validator or WorkflowValidator()).validate(workflow)
        frozen = cast(Mapping[str, object], _freeze(workflow))
        parents = tuple(parent_dna)
        generated = MappingProxyType(dict(generator or {}))
        content = {"dna_spec_version": "1.0", "dna_id": identity,
                   "version": dna_version, "kind": "WORKFLOW", "workflow": _plain(frozen)}
        content_digest = _digest(content)
        envelope = content | {"status": status.value, "content_digest": content_digest,
                              "parent_dna": [item.to_document() for item in parents],
                              "generator": dict(generated)}
        return cls(identity, dna_version, status, frozen, content_digest, _digest(envelope),
                   validation, parents, generated)

    @classmethod
    def from_document(
        cls, document: Mapping[str, object], *, validator: WorkflowValidator | None = None,
    ) -> DnaDefinition:
        try:
            workflow = cast(Mapping[str, object], document["workflow"])
            raw_parents = cast(Iterable[Mapping[str, str]], document["parent_dna"])
            generator = cast(Mapping[str, str], document["generator"])
            rebuilt = cls.from_workflow(
                workflow, dna_id=str(document["dna_id"]), version=str(document["version"]),
                status=DnaStatus(str(document["status"])),
                parent_dna=tuple(DnaParent(str(item["dna_id"]), str(item["version"]),
                                          str(item["content_digest"])) for item in raw_parents),
                generator=generator, validator=validator,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DnaError("DNA document is invalid") from error
        if document.get("dna_spec_version") != "1.0" or document.get("kind") != "WORKFLOW":
            raise DnaError("DNA document version or kind is unsupported")
        if document.get("content_digest") != rebuilt.content_digest:
            raise DnaError("DNA content digest mismatch")
        if document.get("envelope_digest") != rebuilt.envelope_digest:
            raise DnaError("DNA envelope digest mismatch")
        return rebuilt

    def to_document(self) -> dict[str, object]:
        return {
            "dna_spec_version": "1.0", "dna_id": self.dna_id, "version": self.version,
            "kind": "WORKFLOW", "status": self.status.value,
            "content_digest": self.content_digest, "envelope_digest": self.envelope_digest,
            "workflow": _plain(self.workflow),
            "parent_dna": [item.to_document() for item in self.parent_dna],
            "generator": dict(self.generator),
        }

    def with_status(self, status: DnaStatus) -> DnaDefinition:
        envelope = self.to_document() | {"status": status.value}
        envelope.pop("envelope_digest")
        return replace(self, status=status, envelope_digest=_digest(envelope))


class DnaRegistry:
    """Immutable-by-version registry; active versions cannot be overwritten."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], DnaDefinition] = {}

    def register(self, dna: DnaDefinition) -> DnaDefinition:
        key = (dna.dna_id, dna.version)
        if key in self._items:
            raise DnaError(f"DNA version already registered: {dna.dna_id}@{dna.version}")
        self._items[key] = dna
        return dna

    def get(self, dna_id: str, version: str) -> DnaDefinition:
        try:
            return self._items[(dna_id, version)]
        except KeyError as error:
            raise DnaError(f"DNA not found: {dna_id}@{version}") from error

    def activate(self, dna_id: str, version: str) -> DnaDefinition:
        candidate = self.get(dna_id, version)
        if candidate.status not in {DnaStatus.VALIDATED, DnaStatus.CANARY, DnaStatus.ACTIVE}:
            raise DnaError("only VALIDATED, CANARY or ACTIVE DNA can be activated")
        for key, value in tuple(self._items.items()):
            if key[0] == dna_id and value.status is DnaStatus.ACTIVE:
                self._items[key] = value.with_status(DnaStatus.DEPRECATED)
        activated = candidate.with_status(DnaStatus.ACTIVE)
        self._items[(dna_id, version)] = activated
        return activated

    def active(self, dna_id: str) -> DnaDefinition:
        matches = [item for item in self._items.values()
                   if item.dna_id == dna_id and item.status is DnaStatus.ACTIVE]
        if len(matches) != 1:
            raise DnaError(f"DNA has no unique active version: {dna_id}")
        return matches[0]

    def all(self) -> tuple[DnaDefinition, ...]:
        return tuple(self._items.values())


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("DNA must contain JSON-compatible values")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
