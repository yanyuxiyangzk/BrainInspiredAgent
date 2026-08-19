"""Organization DNA for governed multi-Agent roles, routing, arbitration and budgets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.dna import DnaStatus


class OrganizationDnaError(ValueError):
    pass


class DelegationStrategy(StrEnum):
    RESPONSIBILITY = "RESPONSIBILITY"
    PRIORITY = "PRIORITY"


class ArbitrationStrategy(StrEnum):
    PRIORITY = "PRIORITY"
    QUORUM = "QUORUM"
    UNANIMOUS = "UNANIMOUS"


@dataclass(frozen=True, slots=True)
class OrganizationMember:
    role: str
    agent_dna_id: str
    agent_version: str
    agent_content_digest: str
    responsibilities: tuple[str, ...]
    priority: int

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.role) is None
                or not self.agent_dna_id
                or re.fullmatch(r"\d+\.\d+\.\d+", self.agent_version) is None
                or not self.agent_content_digest.startswith("sha256:")
                or not self.responsibilities
                or len(set(self.responsibilities)) != len(self.responsibilities)
                or any(not item for item in self.responsibilities) or self.priority < 0):
            raise OrganizationDnaError("Organization DNA member is invalid")

    def to_document(self) -> dict[str, object]:
        return {"role": self.role, "agent_dna_id": self.agent_dna_id,
                "agent_version": self.agent_version,
                "agent_content_digest": self.agent_content_digest,
                "responsibilities": list(self.responsibilities), "priority": self.priority}


@dataclass(frozen=True, slots=True)
class OrganizationPolicyProfile:
    communication: Mapping[str, object]
    delegation: Mapping[str, object]
    arbitration: Mapping[str, object]
    budget: Mapping[str, object]
    failure: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_profile(self)
        for name in ("communication", "delegation", "arbitration", "budget", "failure"):
            object.__setattr__(self, name, _freeze(cast(Mapping[str, object], getattr(self, name))))

    def to_document(self) -> dict[str, object]:
        return {name: _plain(cast(Mapping[str, object], getattr(self, name)))
                for name in ("communication", "delegation", "arbitration", "budget", "failure")}


@dataclass(frozen=True, slots=True)
class OrganizationDnaDefinition:
    dna_id: str
    version: str
    status: DnaStatus
    profile: OrganizationPolicyProfile
    members: tuple[OrganizationMember, ...]
    content_digest: str
    envelope_digest: str
    generator: Mapping[str, str]

    @classmethod
    def create(
        cls, dna_id: str, version: str, profile: OrganizationPolicyProfile,
        members: Sequence[OrganizationMember], *, status: DnaStatus = DnaStatus.CANDIDATE,
        generator: Mapping[str, str] | None = None,
    ) -> OrganizationDnaDefinition:
        if re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", dna_id) is None \
                or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            raise OrganizationDnaError("Organization DNA identity is invalid")
        fixed = tuple(members)
        if not 2 <= len(fixed) <= 64 or len({item.role for item in fixed}) != len(fixed):
            raise OrganizationDnaError("Organization DNA requires unique multi-Agent roles")
        roles = {item.role for item in fixed}
        tie_break = str(profile.arbitration["tie_break_role"])
        fallback = str(profile.failure["fallback_role"])
        if tie_break not in roles or fallback not in roles:
            raise OrganizationDnaError("Organization DNA policy references an unknown role")
        generated = MappingProxyType(dict(generator or {}))
        content = {"dna_spec_version": "1.0", "dna_id": dna_id, "version": version,
                   "kind": "ORGANIZATION", "profile": profile.to_document(),
                   "members": [item.to_document() for item in fixed]}
        content_digest = _digest(content)
        envelope = content | {"status": status.value, "content_digest": content_digest,
                              "generator": dict(generated)}
        return cls(dna_id, version, status, profile, fixed, content_digest,
                   _digest(envelope), generated)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> OrganizationDnaDefinition:
        try:
            raw = cast(Mapping[str, Mapping[str, object]], document["profile"])
            profile = OrganizationPolicyProfile(**{name: raw[name] for name in (
                "communication", "delegation", "arbitration", "budget", "failure",
            )})
            members = tuple(OrganizationMember(
                str(item["role"]), str(item["agent_dna_id"]), str(item["agent_version"]),
                str(item["agent_content_digest"]),
                tuple(cast(Sequence[str], item["responsibilities"])),
                cast(int, item["priority"]),
            ) for item in cast(Sequence[Mapping[str, object]], document["members"]))
            rebuilt = cls.create(
                str(document["dna_id"]), str(document["version"]), profile, members,
                status=DnaStatus(str(document["status"])),
                generator=cast(Mapping[str, str], document["generator"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OrganizationDnaError("Organization DNA document is invalid") from error
        if document.get("dna_spec_version") != "1.0" or document.get("kind") != "ORGANIZATION":
            raise OrganizationDnaError("Organization DNA kind or version is unsupported")
        if document.get("content_digest") != rebuilt.content_digest:
            raise OrganizationDnaError("Organization DNA content digest mismatch")
        if document.get("envelope_digest") != rebuilt.envelope_digest:
            raise OrganizationDnaError("Organization DNA envelope digest mismatch")
        return rebuilt

    def to_document(self) -> dict[str, object]:
        return {"dna_spec_version": "1.0", "dna_id": self.dna_id, "version": self.version,
                "kind": "ORGANIZATION", "status": self.status.value,
                "content_digest": self.content_digest, "envelope_digest": self.envelope_digest,
                "profile": self.profile.to_document(),
                "members": [item.to_document() for item in self.members],
                "generator": dict(self.generator)}

    def with_status(self, status: DnaStatus) -> OrganizationDnaDefinition:
        document = self.to_document() | {"status": status.value}
        document.pop("envelope_digest")
        return replace(self, status=status, envelope_digest=_digest(document))

    def delegate(self, responsibility: str, *,
                 unavailable_roles: frozenset[str] = frozenset()) -> OrganizationMember:
        candidates = [member for member in self.members if member.role not in unavailable_roles
                      and responsibility in member.responsibilities]
        if not candidates:
            fallback = str(self.profile.failure["fallback_role"])
            candidates = [member for member in self.members
                          if member.role == fallback and member.role not in unavailable_roles]
        if not candidates:
            raise OrganizationDnaError("Organization has no available delegate")
        return min(candidates, key=lambda item: (-item.priority, item.role))

    def arbitrate(self, votes: Mapping[str, bool]) -> bool:
        roles = {member.role for member in self.members}
        if not votes or not set(votes) <= roles:
            raise OrganizationDnaError("Organization arbitration contains unknown or empty votes")
        strategy = ArbitrationStrategy(str(self.profile.arbitration["strategy"]))
        if strategy is ArbitrationStrategy.PRIORITY:
            role = str(self.profile.arbitration["tie_break_role"])
            if role not in votes:
                raise OrganizationDnaError("Organization priority arbiter did not vote")
            return votes[role]
        if strategy is ArbitrationStrategy.UNANIMOUS:
            return len(votes) == len(self.members) and all(votes.values())
        ratio = sum(votes.values()) / len(self.members)
        return ratio >= cast(float, self.profile.arbitration["quorum_ratio"])

    def approve_budget(self, *, tokens: int, cost_minor: int, duration_seconds: int,
                       parallel_agents: int) -> None:
        requested = (tokens, cost_minor, duration_seconds, parallel_agents)
        if any(item < 0 for item in requested):
            raise OrganizationDnaError("Organization requested budget is negative")
        policy = self.profile.budget
        limits = (cast(int, policy["max_tokens"]), cast(int, policy["max_cost_minor"]),
                  cast(int, policy["max_duration_seconds"]),
                  cast(int, policy["max_parallel_agents"]))
        if any(value > limit for value, limit in zip(requested, limits, strict=True)):
            raise OrganizationDnaError("Organization budget exceeded")


@dataclass(frozen=True, slots=True)
class PersistentOrganizationDnaRecord:
    dna: OrganizationDnaDefinition
    revision: int


_ALLOWED = {DnaStatus.CANDIDATE: frozenset({DnaStatus.VALIDATED, DnaStatus.RETIRED}),
            DnaStatus.VALIDATED: frozenset({DnaStatus.ACTIVE, DnaStatus.RETIRED}),
            DnaStatus.ACTIVE: frozenset({DnaStatus.DEPRECATED}),
            DnaStatus.DEPRECATED: frozenset({DnaStatus.RETIRED}),
            DnaStatus.RETIRED: frozenset()}


class PersistentOrganizationDnaRegistry:
    def __init__(self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator) -> None:
        self._database, self._clock, self._identifiers = database, clock, identifiers

    async def register(self, dna: OrganizationDnaDefinition, *,
                       correlation_id: str) -> PersistentOrganizationDnaRecord:
        if dna.status is not DnaStatus.CANDIDATE:
            raise OrganizationDnaError("new Organization DNA must be CANDIDATE")
        now = _time(self._clock.now())
        async with self._database.transaction() as transaction:
            for member in dna.members:
                row = await transaction.fetch_one(
                    """SELECT status,content_digest FROM agent_dna_definition
                       WHERE dna_id=? AND version=?""",
                    (member.agent_dna_id, member.agent_version),
                )
                if (row is None or str(row["content_digest"]) != member.agent_content_digest
                        or str(row["status"]) not in {"VALIDATED", "ACTIVE", "DEPRECATED"}):
                    raise OrganizationDnaError(
                        "Organization references an unavailable Agent DNA",
                    )
            await transaction.execute(
                "INSERT INTO organization_dna_definition VALUES (?,?,?,?,?,?,0,?,?,?)",
                (dna.dna_id, dna.version, dna.status.value, dna.content_digest,
                 dna.envelope_digest, _json(dna.to_document()), now, now, correlation_id),
            )
            await transaction.executemany(
                "INSERT INTO organization_dna_member VALUES (?,?,?,?,?,?,?,?)",
                tuple((dna.dna_id, dna.version, member.role, member.agent_dna_id,
                       member.agent_version, member.agent_content_digest,
                       _json(list(member.responsibilities)), member.priority)
                      for member in dna.members),
            )
            await self._event(transaction, dna, None, DnaStatus.CANDIDATE, None, 0,
                              "registered", correlation_id)
        return PersistentOrganizationDnaRecord(dna, 0)

    async def get(self, dna_id: str, version: str) -> PersistentOrganizationDnaRecord:
        row = await self._database.fetch_one(
            """SELECT document_json,revision FROM organization_dna_definition
               WHERE dna_id=? AND version=?""", (dna_id, version),
        )
        if row is None:
            raise OrganizationDnaError(f"Organization DNA not found: {dna_id}@{version}")
        return PersistentOrganizationDnaRecord(
            OrganizationDnaDefinition.from_document(json.loads(str(row["document_json"]))),
            int(row["revision"]),
        )

    async def transition(self, dna_id: str, version: str, status: DnaStatus, *,
                         expected_revision: int, reason: str,
                         correlation_id: str) -> PersistentOrganizationDnaRecord:
        async with self._database.transaction() as transaction:
            row = await transaction.fetch_one(
                """SELECT document_json,revision FROM organization_dna_definition
                   WHERE dna_id=? AND version=?""", (dna_id, version),
            )
            if row is None:
                raise OrganizationDnaError(f"Organization DNA not found: {dna_id}@{version}")
            current = PersistentOrganizationDnaRecord(
                OrganizationDnaDefinition.from_document(json.loads(str(row["document_json"]))),
                int(row["revision"]),
            )
            if current.revision != expected_revision:
                raise OrganizationDnaError("Organization DNA revision conflict")
            if status not in _ALLOWED.get(current.dna.status, frozenset()):
                raise OrganizationDnaError("illegal Organization DNA transition")
            if status is DnaStatus.ACTIVE:
                active = await transaction.fetch_one(
                    """SELECT document_json,revision FROM organization_dna_definition
                       WHERE dna_id=? AND status='ACTIVE'""", (dna_id,),
                )
                if active is not None:
                    previous = PersistentOrganizationDnaRecord(
                        OrganizationDnaDefinition.from_document(
                            json.loads(str(active["document_json"]))), int(active["revision"]),
                    )
                    if previous.dna.version != version:
                        await self._change(transaction, previous, DnaStatus.DEPRECATED,
                                           "replaced by Organization DNA", correlation_id)
            return await self._change(transaction, current, status, reason, correlation_id)

    async def active(self, dna_id: str) -> PersistentOrganizationDnaRecord:
        row = await self._database.fetch_one(
            "SELECT version FROM organization_dna_definition WHERE dna_id=? AND status='ACTIVE'",
            (dna_id,),
        )
        if row is None:
            raise OrganizationDnaError(f"Organization DNA has no active version: {dna_id}")
        return await self.get(dna_id, str(row["version"]))

    async def _change(self, transaction: SQLiteTransaction,
                      current: PersistentOrganizationDnaRecord, status: DnaStatus,
                      reason: str, correlation_id: str) -> PersistentOrganizationDnaRecord:
        changed, revision = current.dna.with_status(status), current.revision + 1
        cursor = await transaction.execute(
            """UPDATE organization_dna_definition SET status=?,envelope_digest=?,document_json=?,
                      revision=?,updated_at=? WHERE dna_id=? AND version=? AND revision=?""",
            (status.value, changed.envelope_digest, _json(changed.to_document()), revision,
             _time(self._clock.now()), changed.dna_id, changed.version, current.revision),
        )
        if cursor.rowcount != 1:
            raise OrganizationDnaError("Organization DNA revision conflict")
        await self._event(transaction, changed, current.dna.status, status, current.revision,
                          revision, reason, correlation_id)
        return PersistentOrganizationDnaRecord(changed, revision)

    async def _event(self, transaction: SQLiteTransaction, dna: OrganizationDnaDefinition,
                     previous: DnaStatus | None, status: DnaStatus,
                     previous_revision: int | None, revision: int, reason: str,
                     correlation_id: str) -> None:
        await transaction.execute(
            "INSERT INTO organization_dna_transition VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(self._identifiers.new()), dna.dna_id, dna.version,
             None if previous is None else previous.value, status.value, previous_revision,
             revision, reason, _time(self._clock.now()), correlation_id),
        )


def _validate_profile(profile: OrganizationPolicyProfile) -> None:
    expected = {"communication": {"channels", "max_message_bytes", "max_hops"},
                "delegation": {"strategy", "max_inflight_per_agent"},
                "arbitration": {"strategy", "quorum_ratio", "tie_break_role"},
                "budget": {"max_tokens", "max_cost_minor", "max_duration_seconds",
                           "max_parallel_agents"},
                "failure": {"max_member_failures", "isolation_seconds", "fallback_role"}}
    for name, fields in expected.items():
        if set(cast(Mapping[str, object], getattr(profile, name))) != fields:
            raise OrganizationDnaError(f"Organization DNA {name} policy fields are invalid")
    if (profile.delegation["strategy"] not in set(DelegationStrategy)
            or profile.arbitration["strategy"] not in set(ArbitrationStrategy)):
        raise OrganizationDnaError("Organization DNA strategy is invalid")
    if (not isinstance(profile.communication["channels"], Sequence)
            or isinstance(profile.communication["channels"], str | bytes)
            or not cast(Sequence[object], profile.communication["channels"])):
        raise OrganizationDnaError("Organization DNA channels are invalid")
    integers = (profile.communication["max_message_bytes"], profile.communication["max_hops"],
                profile.delegation["max_inflight_per_agent"], profile.budget["max_tokens"],
                profile.budget["max_cost_minor"], profile.budget["max_duration_seconds"],
                profile.budget["max_parallel_agents"], profile.failure["max_member_failures"],
                profile.failure["isolation_seconds"])
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in integers):
        raise OrganizationDnaError("Organization DNA limits must be positive integers")
    ratio = profile.arbitration["quorum_ratio"]
    if not isinstance(ratio, int | float) or isinstance(ratio, bool) or not 0 < ratio <= 1:
        raise OrganizationDnaError("Organization DNA quorum ratio is invalid")


def _freeze(value: Mapping[str, object]) -> Mapping[str, object]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(value) for key, value in item.items()})
        if isinstance(item, list | tuple):
            return tuple(freeze(value) for value in item)
        if item is None or isinstance(item, str | int | float | bool):
            return item
        raise OrganizationDnaError("Organization DNA policy must be JSON-compatible")
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
        raise OrganizationDnaError("Organization DNA time must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
