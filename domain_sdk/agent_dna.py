"""Versioned Agent DNA policies referencing governed Workflow DNA."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.dna import DnaStatus


class AgentDnaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowDnaReference:
    role: str
    dna_id: str
    version: str
    content_digest: str

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.role) is None
                or not self.dna_id or re.fullmatch(r"\d+\.\d+\.\d+", self.version) is None
                or not self.content_digest.startswith("sha256:")):
            raise AgentDnaError("Agent DNA Workflow reference is invalid")

    def to_document(self) -> dict[str, str]:
        return {"role": self.role, "dna_id": self.dna_id, "version": self.version,
                "content_digest": self.content_digest}


@dataclass(frozen=True, slots=True)
class AgentPolicyProfile:
    goal: Mapping[str, object]
    attention: Mapping[str, object]
    planning: Mapping[str, object]
    memory: Mapping[str, object]
    evaluation: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_profile(self)
        for name in ("goal", "attention", "planning", "memory", "evaluation"):
            object.__setattr__(self, name, _freeze(cast(Mapping[str, object], getattr(self, name))))

    def to_document(self) -> dict[str, object]:
        return {name: _plain(cast(Mapping[str, object], getattr(self, name)))
                for name in ("goal", "attention", "planning", "memory", "evaluation")}


@dataclass(frozen=True, slots=True)
class AgentDnaDefinition:
    dna_id: str
    version: str
    status: DnaStatus
    profile: AgentPolicyProfile
    workflow_dna: tuple[WorkflowDnaReference, ...]
    content_digest: str
    envelope_digest: str
    generator: Mapping[str, str]

    @classmethod
    def create(
        cls, dna_id: str, version: str, profile: AgentPolicyProfile,
        workflow_dna: Sequence[WorkflowDnaReference], *,
        status: DnaStatus = DnaStatus.CANDIDATE,
        generator: Mapping[str, str] | None = None,
    ) -> AgentDnaDefinition:
        if re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", dna_id) is None \
                or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            raise AgentDnaError("Agent DNA identity is invalid")
        refs = tuple(workflow_dna)
        if not refs or len(refs) > 32 or len({item.role for item in refs}) != len(refs):
            raise AgentDnaError("Agent DNA Workflow roles must be non-empty and unique")
        generated = MappingProxyType(dict(generator or {}))
        content = {"dna_spec_version": "1.0", "dna_id": dna_id, "version": version,
                   "kind": "AGENT", "profile": profile.to_document(),
                   "workflow_dna": [item.to_document() for item in refs]}
        content_digest = _digest(content)
        envelope = content | {"status": status.value, "content_digest": content_digest,
                              "generator": dict(generated)}
        return cls(dna_id, version, status, profile, refs, content_digest,
                   _digest(envelope), generated)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> AgentDnaDefinition:
        try:
            profile_doc = cast(Mapping[str, Mapping[str, object]], document["profile"])
            profile = AgentPolicyProfile(**{name: profile_doc[name] for name in (
                "goal", "attention", "planning", "memory", "evaluation",
            )})
            refs = tuple(WorkflowDnaReference(**cast(dict[str, str], item)) for item in
                         cast(Sequence[Mapping[str, object]], document["workflow_dna"]))
            rebuilt = cls.create(
                str(document["dna_id"]), str(document["version"]), profile, refs,
                status=DnaStatus(str(document["status"])),
                generator=cast(Mapping[str, str], document["generator"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AgentDnaError("Agent DNA document is invalid") from error
        if document.get("dna_spec_version") != "1.0" or document.get("kind") != "AGENT":
            raise AgentDnaError("Agent DNA document kind or version is unsupported")
        if document.get("content_digest") != rebuilt.content_digest:
            raise AgentDnaError("Agent DNA content digest mismatch")
        if document.get("envelope_digest") != rebuilt.envelope_digest:
            raise AgentDnaError("Agent DNA envelope digest mismatch")
        return rebuilt

    def to_document(self) -> dict[str, object]:
        return {"dna_spec_version": "1.0", "dna_id": self.dna_id, "version": self.version,
                "kind": "AGENT", "status": self.status.value,
                "content_digest": self.content_digest, "envelope_digest": self.envelope_digest,
                "profile": self.profile.to_document(),
                "workflow_dna": [item.to_document() for item in self.workflow_dna],
                "generator": dict(self.generator)}

    def with_status(self, status: DnaStatus) -> AgentDnaDefinition:
        document = self.to_document() | {"status": status.value}
        document.pop("envelope_digest")
        return replace(self, status=status, envelope_digest=_digest(document))


@dataclass(frozen=True, slots=True)
class PersistentAgentDnaRecord:
    dna: AgentDnaDefinition
    revision: int


_ALLOWED = {
    DnaStatus.CANDIDATE: frozenset({DnaStatus.VALIDATED, DnaStatus.RETIRED}),
    DnaStatus.VALIDATED: frozenset({DnaStatus.ACTIVE, DnaStatus.RETIRED}),
    DnaStatus.ACTIVE: frozenset({DnaStatus.DEPRECATED}),
    DnaStatus.DEPRECATED: frozenset({DnaStatus.RETIRED}),
    DnaStatus.RETIRED: frozenset(), DnaStatus.SHADOW: frozenset(),
    DnaStatus.CANARY: frozenset(),
}


class PersistentAgentDnaRegistry:
    def __init__(self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator) -> None:
        self._database, self._clock, self._identifiers = database, clock, identifiers

    async def register(self, dna: AgentDnaDefinition, *,
                       correlation_id: str) -> PersistentAgentDnaRecord:
        if dna.status is not DnaStatus.CANDIDATE:
            raise AgentDnaError("new Agent DNA must be CANDIDATE")
        now = _time(self._clock.now())
        async with self._database.transaction() as transaction:
            for ref in dna.workflow_dna:
                row = await transaction.fetch_one(
                    """SELECT status,content_digest FROM dna_definition
                       WHERE dna_id=? AND version=?""", (ref.dna_id, ref.version),
                )
                if (row is None or str(row["content_digest"]) != ref.content_digest
                        or str(row["status"]) in {"CANDIDATE", "RETIRED"}):
                    raise AgentDnaError("Agent DNA references an unavailable Workflow DNA")
            await transaction.execute(
                "INSERT INTO agent_dna_definition VALUES (?,?,?,?,?,?,0,?,?,?)",
                (dna.dna_id, dna.version, dna.status.value, dna.content_digest,
                 dna.envelope_digest, _json(dna.to_document()), now, now, correlation_id),
            )
            await transaction.executemany(
                "INSERT INTO agent_dna_workflow_ref VALUES (?,?,?,?,?,?)",
                tuple((dna.dna_id, dna.version, ref.role, ref.dna_id, ref.version,
                       ref.content_digest) for ref in dna.workflow_dna),
            )
            await self._event(transaction, dna, None, DnaStatus.CANDIDATE, None, 0,
                              "registered", correlation_id)
        return PersistentAgentDnaRecord(dna, 0)

    async def get(self, dna_id: str, version: str) -> PersistentAgentDnaRecord:
        row = await self._database.fetch_one(
            "SELECT document_json,revision FROM agent_dna_definition WHERE dna_id=? AND version=?",
            (dna_id, version),
        )
        if row is None:
            raise AgentDnaError(f"Agent DNA not found: {dna_id}@{version}")
        return PersistentAgentDnaRecord(
            AgentDnaDefinition.from_document(json.loads(str(row["document_json"]))),
            int(row["revision"]),
        )

    async def transition(self, dna_id: str, version: str, status: DnaStatus, *,
                         expected_revision: int, reason: str,
                         correlation_id: str) -> PersistentAgentDnaRecord:
        async with self._database.transaction() as transaction:
            row = await transaction.fetch_one(
                "SELECT document_json,revision FROM agent_dna_definition WHERE dna_id=? AND version=?",
                (dna_id, version),
            )
            if row is None:
                raise AgentDnaError(f"Agent DNA not found: {dna_id}@{version}")
            current = PersistentAgentDnaRecord(
                AgentDnaDefinition.from_document(json.loads(str(row["document_json"]))),
                int(row["revision"]),
            )
            if current.revision != expected_revision:
                raise AgentDnaError("Agent DNA revision conflict")
            if status not in _ALLOWED[current.dna.status]:
                raise AgentDnaError("illegal Agent DNA transition")
            if status is DnaStatus.ACTIVE:
                active = await transaction.fetch_one(
                    """SELECT version,document_json,revision FROM agent_dna_definition
                       WHERE dna_id=? AND status='ACTIVE'""", (dna_id,),
                )
                if active is not None and str(active["version"]) != version:
                    old = AgentDnaDefinition.from_document(json.loads(str(active["document_json"])))
                    await self._change(transaction, PersistentAgentDnaRecord(
                        old, int(active["revision"])), DnaStatus.DEPRECATED,
                        "replaced by Agent DNA", correlation_id)
            return await self._change(transaction, current, status, reason, correlation_id)

    async def active(self, dna_id: str) -> PersistentAgentDnaRecord:
        row = await self._database.fetch_one(
            "SELECT version FROM agent_dna_definition WHERE dna_id=? AND status='ACTIVE'",
            (dna_id,),
        )
        if row is None:
            raise AgentDnaError(f"Agent DNA has no active version: {dna_id}")
        return await self.get(dna_id, str(row["version"]))

    async def _change(self, transaction: SQLiteTransaction, current: PersistentAgentDnaRecord,
                      status: DnaStatus, reason: str,
                      correlation_id: str) -> PersistentAgentDnaRecord:
        changed, revision = current.dna.with_status(status), current.revision + 1
        cursor = await transaction.execute(
            """UPDATE agent_dna_definition SET status=?,envelope_digest=?,document_json=?,
                      revision=?,updated_at=? WHERE dna_id=? AND version=? AND revision=?""",
            (status.value, changed.envelope_digest, _json(changed.to_document()), revision,
             _time(self._clock.now()), changed.dna_id, changed.version, current.revision),
        )
        if cursor.rowcount != 1:
            raise AgentDnaError("Agent DNA revision conflict")
        await self._event(transaction, changed, current.dna.status, status, current.revision,
                          revision, reason, correlation_id)
        return PersistentAgentDnaRecord(changed, revision)

    async def _event(self, transaction: SQLiteTransaction, dna: AgentDnaDefinition,
                     previous: DnaStatus | None, status: DnaStatus,
                     previous_revision: int | None, revision: int, reason: str,
                     correlation_id: str) -> None:
        await transaction.execute(
            "INSERT INTO agent_dna_transition VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(self._identifiers.new()), dna.dna_id, dna.version,
             None if previous is None else previous.value, status.value, previous_revision,
             revision, reason, _time(self._clock.now()), correlation_id),
        )


def _validate_profile(profile: AgentPolicyProfile) -> None:
    expected = {
        "goal": {"allowed_goal_types", "max_active_goals", "default_priority"},
        "attention": {"salience_weights", "max_focus_items", "switch_threshold"},
        "planning": {"strategy", "horizon_seconds", "max_tasks"},
        "memory": {"working_items", "episodic_retention_days", "semantic_candidates"},
        "evaluation": {"minimum_evidence_score", "minimum_value_score",
                       "review_interval_seconds"},
    }
    for name, fields in expected.items():
        value = cast(Mapping[str, object], getattr(profile, name))
        if set(value) != fields:
            raise AgentDnaError(f"Agent DNA {name} policy fields are invalid")
    goal, attention, planning = profile.goal, profile.attention, profile.planning
    memory, evaluation = profile.memory, profile.evaluation
    if (not isinstance(goal["allowed_goal_types"], Sequence)
            or isinstance(goal["allowed_goal_types"], str | bytes)
            or not cast(Sequence[object], goal["allowed_goal_types"])
            or any(not isinstance(item, str) for item in
                   cast(Sequence[object], goal["allowed_goal_types"]))):
        raise AgentDnaError("Agent DNA goal types are invalid")
    integers = (goal["max_active_goals"], attention["max_focus_items"],
                planning["horizon_seconds"], planning["max_tasks"], memory["working_items"],
                memory["episodic_retention_days"], memory["semantic_candidates"],
                evaluation["review_interval_seconds"])
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in integers):
        raise AgentDnaError("Agent DNA policy limits must be positive integers")
    if planning["strategy"] not in {"REACTIVE", "DELIBERATIVE", "HYBRID"}:
        raise AgentDnaError("Agent DNA planning strategy is invalid")
    scores = (goal["default_priority"], attention["switch_threshold"],
              evaluation["minimum_evidence_score"], evaluation["minimum_value_score"])
    if any(not isinstance(item, int | float) or isinstance(item, bool)
           or not 0 <= item <= 1 for item in scores):
        raise AgentDnaError("Agent DNA policy scores are invalid")
    weights = attention["salience_weights"]
    if (not isinstance(weights, Mapping) or not weights
            or any(not isinstance(value, int | float) or isinstance(value, bool) or value < 0
                   for value in weights.values())):
        raise AgentDnaError("Agent DNA salience weights are invalid")


def _freeze(value: Mapping[str, object]) -> Mapping[str, object]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(value) for key, value in item.items()})
        if isinstance(item, list | tuple):
            return tuple(freeze(value) for value in item)
        if item is None or isinstance(item, str | int | float | bool):
            return item
        raise AgentDnaError("Agent DNA policy must be JSON-compatible")
    return cast(Mapping[str, object], freeze(value))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AgentDnaError("Agent DNA time must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
