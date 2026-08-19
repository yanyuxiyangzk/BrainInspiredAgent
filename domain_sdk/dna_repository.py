"""Transactional, persistent DNA registry with CAS and append-only transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.dna import DnaDefinition, DnaError, DnaStatus


@dataclass(frozen=True, slots=True)
class PersistentDnaRecord:
    dna: DnaDefinition
    revision: int


_ALLOWED = {
    DnaStatus.CANDIDATE: frozenset({DnaStatus.VALIDATED, DnaStatus.RETIRED}),
    DnaStatus.VALIDATED: frozenset({DnaStatus.SHADOW, DnaStatus.RETIRED}),
    DnaStatus.SHADOW: frozenset({DnaStatus.CANARY, DnaStatus.RETIRED}),
    DnaStatus.CANARY: frozenset({DnaStatus.ACTIVE, DnaStatus.RETIRED}),
    DnaStatus.ACTIVE: frozenset({DnaStatus.DEPRECATED}),
    DnaStatus.DEPRECATED: frozenset({DnaStatus.RETIRED}),
    DnaStatus.RETIRED: frozenset(),
}


class PersistentDnaRegistry:
    def __init__(self, database: SQLiteDatabase, clock: Clock, identifiers: UuidGenerator) -> None:
        self._database, self._clock, self._identifiers = database, clock, identifiers

    async def register(self, dna: DnaDefinition, *, correlation_id: str) -> PersistentDnaRecord:
        if dna.status is not DnaStatus.CANDIDATE:
            raise DnaError("new persistent DNA must be CANDIDATE")
        now = _time(self._clock.now())
        async with self._database.transaction() as transaction:
            await self._validate_parents(transaction, dna)
            await transaction.execute(
                """INSERT INTO dna_definition(
                       dna_id,version,kind,status,content_digest,envelope_digest,document_json,
                       revision,created_at,updated_at,correlation_id
                   ) VALUES (?,?,?,?,?,?,?,0,?,?,?)""",
                (dna.dna_id, dna.version, "WORKFLOW", dna.status.value, dna.content_digest,
                 dna.envelope_digest, _json(dna.to_document()), now, now, correlation_id),
            )
            for ordinal, parent in enumerate(dna.parent_dna):
                await transaction.execute(
                    "INSERT INTO dna_parent VALUES (?,?,?,?,?,?)",
                    (dna.dna_id, dna.version, ordinal, parent.dna_id, parent.version,
                     parent.content_digest),
                )
            await self._append_transition(transaction, dna.dna_id, dna.version, None,
                                          DnaStatus.CANDIDATE, None, 0, "registered",
                                          correlation_id)
        return PersistentDnaRecord(dna, 0)

    async def get(self, dna_id: str, version: str) -> PersistentDnaRecord:
        row = await self._database.fetch_one(
            "SELECT document_json,revision FROM dna_definition WHERE dna_id=? AND version=?",
            (dna_id, version),
        )
        if row is None:
            raise DnaError(f"DNA not found: {dna_id}@{version}")
        return PersistentDnaRecord(DnaDefinition.from_document(json.loads(str(row["document_json"]))),
                                   int(row["revision"]))

    async def transition(
        self, dna_id: str, version: str, to_status: DnaStatus, *, expected_revision: int,
        reason: str, correlation_id: str,
    ) -> PersistentDnaRecord:
        async with self._database.transaction() as transaction:
            record = await self._get(transaction, dna_id, version)
            if to_status not in _ALLOWED[record.dna.status]:
                raise DnaError(f"illegal DNA transition: {record.dna.status}->{to_status}")
            return await self._change(transaction, record, to_status, expected_revision,
                                      reason, correlation_id)

    async def activate(
        self, dna_id: str, version: str, *, expected_revision: int,
        reason: str, correlation_id: str,
    ) -> PersistentDnaRecord:
        async with self._database.transaction() as transaction:
            candidate = await self._get(transaction, dna_id, version)
            if candidate.dna.status is not DnaStatus.CANARY:
                raise DnaError("only CANARY DNA can be activated")
            current_row = await transaction.fetch_one(
                "SELECT version,revision FROM dna_definition WHERE dna_id=? AND status='ACTIVE'",
                (dna_id,),
            )
            if current_row is not None and str(current_row["version"]) != version:
                current = await self._get(transaction, dna_id, str(current_row["version"]))
                await self._change(transaction, current, DnaStatus.DEPRECATED, current.revision,
                                   "replaced by new active DNA", correlation_id)
            return await self._change(transaction, candidate, DnaStatus.ACTIVE, expected_revision,
                                      reason, correlation_id)

    async def rollback(
        self, dna_id: str, target_version: str, *, expected_active_revision: int,
        expected_target_revision: int, reason: str, correlation_id: str,
    ) -> PersistentDnaRecord:
        async with self._database.transaction() as transaction:
            active_row = await transaction.fetch_one(
                "SELECT version FROM dna_definition WHERE dna_id=? AND status='ACTIVE'", (dna_id,)
            )
            if active_row is None:
                raise DnaError("DNA rollback requires an active version")
            active = await self._get(transaction, dna_id, str(active_row["version"]))
            target = await self._get(transaction, dna_id, target_version)
            if target.dna.status is not DnaStatus.DEPRECATED:
                raise DnaError("DNA rollback target must be DEPRECATED")
            await self._change(transaction, active, DnaStatus.DEPRECATED,
                               expected_active_revision, reason, correlation_id)
            return await self._change(transaction, target, DnaStatus.ACTIVE,
                                      expected_target_revision, reason, correlation_id,
                                      allow_rollback=True)

    async def history(self, dna_id: str, version: str) -> tuple[dict[str, object], ...]:
        rows = await self._database.fetch_all(
            "SELECT * FROM dna_transition WHERE dna_id=? AND version=? ORDER BY rowid",
            (dna_id, version),
        )
        return tuple(dict(row) for row in rows)

    async def _validate_parents(self, transaction: SQLiteTransaction, dna: DnaDefinition) -> None:
        if len(dna.parent_dna) > 4:
            raise DnaError("DNA has too many parents")
        child = (dna.dna_id, dna.version)
        for parent in dna.parent_dna:
            if (parent.dna_id, parent.version) == child:
                raise DnaError("DNA lineage cycle detected")
            row = await transaction.fetch_one(
                "SELECT content_digest FROM dna_definition WHERE dna_id=? AND version=?",
                (parent.dna_id, parent.version),
            )
            if row is None or str(row["content_digest"]) != parent.content_digest:
                raise DnaError("DNA parent is missing or digest does not match")
            await self._assert_acyclic(transaction, child, (parent.dna_id, parent.version))

    async def _assert_acyclic(
        self, transaction: SQLiteTransaction, child: tuple[str, str], start: tuple[str, str],
    ) -> None:
        pending = [(start, 0, frozenset[tuple[str, str]]())]
        while pending:
            identity, depth, ancestors = pending.pop()
            if identity == child or identity in ancestors:
                raise DnaError("DNA lineage cycle detected")
            if depth >= 32:
                raise DnaError("DNA lineage exceeds maximum depth")
            rows = await transaction.fetch_all(
                "SELECT parent_dna_id,parent_version FROM dna_parent WHERE child_dna_id=? AND child_version=?",
                identity,
            )
            path = ancestors | {identity}
            pending.extend(
                ((str(row["parent_dna_id"]), str(row["parent_version"])), depth + 1, path)
                for row in rows
            )

    async def _get(self, transaction: SQLiteTransaction, dna_id: str,
                   version: str) -> PersistentDnaRecord:
        row = await transaction.fetch_one(
            "SELECT document_json,revision FROM dna_definition WHERE dna_id=? AND version=?",
            (dna_id, version),
        )
        if row is None:
            raise DnaError(f"DNA not found: {dna_id}@{version}")
        dna = DnaDefinition.from_document(json.loads(str(row["document_json"])))
        return PersistentDnaRecord(dna, int(row["revision"]))

    async def _change(
        self, transaction: SQLiteTransaction, record: PersistentDnaRecord, to_status: DnaStatus,
        expected_revision: int, reason: str, correlation_id: str, *, allow_rollback: bool = False,
    ) -> PersistentDnaRecord:
        if record.revision != expected_revision:
            raise DnaError("DNA revision conflict")
        if not allow_rollback and to_status not in _ALLOWED[record.dna.status]:
            raise DnaError(f"illegal DNA transition: {record.dna.status}->{to_status}")
        changed = record.dna.with_status(to_status)
        revision = record.revision + 1
        cursor = await transaction.execute(
            """UPDATE dna_definition SET status=?,envelope_digest=?,document_json=?,revision=?,updated_at=?
               WHERE dna_id=? AND version=? AND revision=?""",
            (to_status.value, changed.envelope_digest, _json(changed.to_document()), revision,
             _time(self._clock.now()), changed.dna_id, changed.version, expected_revision),
        )
        if cursor.rowcount != 1:
            raise DnaError("DNA revision conflict")
        await self._append_transition(transaction, changed.dna_id, changed.version,
                                      record.dna.status, to_status, record.revision, revision,
                                      reason, correlation_id)
        return PersistentDnaRecord(changed, revision)

    async def _append_transition(
        self, transaction: SQLiteTransaction, dna_id: str, version: str,
        from_status: DnaStatus | None, to_status: DnaStatus, from_revision: int | None,
        to_revision: int, reason: str, correlation_id: str,
    ) -> None:
        identity = str(self._identifiers.new())
        await transaction.execute(
            "INSERT INTO dna_transition VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity, dna_id, version, None if from_status is None else from_status.value,
             to_status.value, from_revision, to_revision, reason, identity,
             _time(self._clock.now()), correlation_id),
        )


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
