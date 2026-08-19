from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import (
    DnaDefinition,
    DnaError,
    DnaParent,
    DnaStatus,
    PersistentDnaRegistry,
)

NOW = datetime(2026, 8, 18, 8, tzinfo=UTC)


def workflow(version: str) -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "agent_market", "version": version,
        "name": "Market agent", "input_schema": {"type": "object"},
        "policy": {"timeout_seconds": 10, "max_parallelism": 1,
                   "required_capabilities": ["market.summarize"]},
        "nodes": [{"node_id": "summary", "type": "skill", "depends_on": [],
                   "capability": "market.summarize", "capability_version": "1.0",
                   "input": {}, "constraints": {"side_effect": "PURE"}}],
        "output_mapping": {"summary": "$.nodes.summary.output"},
    }


def dna(version: str, *parents: DnaDefinition) -> DnaDefinition:
    return DnaDefinition.from_workflow(
        workflow(version), version=version,
        parent_dna=tuple(DnaParent(item.dna_id, item.version, item.content_digest)
                         for item in parents),
    )


async def registry(path: Path) -> tuple[SQLiteDatabase, PersistentDnaRegistry]:
    database = SQLiteDatabase(path)
    await database.initialize()
    identifiers = FakeUuidGenerator(UUID(int=value) for value in range(1, 100))
    return database, PersistentDnaRegistry(database, FakeClock(NOW), identifiers)


async def promote(
    service: PersistentDnaRegistry, definition: DnaDefinition, correlation: str,
) -> None:
    record = await service.register(definition, correlation_id=correlation)
    for status in (DnaStatus.VALIDATED, DnaStatus.SHADOW, DnaStatus.CANARY):
        record = await service.transition(
            definition.dna_id, definition.version, status,
            expected_revision=record.revision, reason="gate passed", correlation_id=correlation,
        )


@pytest.mark.asyncio
async def test_registry_survives_restart_and_rejects_tampered_document(tmp_path: Path) -> None:
    path = tmp_path / "dna.db"
    database, service = await registry(path)
    definition = dna("1.0.0")
    await service.register(definition, correlation_id="corr-register")
    assert len(await service.history(definition.dna_id, definition.version)) == 1
    await database.close()

    restarted, service = await registry(path)
    restored = await service.get(definition.dna_id, definition.version)
    assert restored == type(restored)(definition, 0)
    document = definition.to_document()
    document["workflow"] = workflow("9.9.9")
    async with restarted.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET document_json=? WHERE dna_id=? AND version=?",
            (json.dumps(document), definition.dna_id, definition.version),
        )
    with pytest.raises(DnaError, match="digest mismatch"):
        await service.get(definition.dna_id, definition.version)
    await restarted.close()


@pytest.mark.asyncio
async def test_parent_existence_digest_limits_and_cycle_are_enforced(tmp_path: Path) -> None:
    database, service = await registry(tmp_path / "parents.db")
    root = dna("1.0.0")
    await service.register(root, correlation_id="root")
    missing = DnaDefinition.from_workflow(
        workflow("2.0.0"), version="2.0.0",
        parent_dna=(DnaParent(root.dna_id, "0.0.1", root.content_digest),),
    )
    with pytest.raises(DnaError, match="missing or digest"):
        await service.register(missing, correlation_id="missing")
    wrong = DnaDefinition.from_workflow(
        workflow("2.0.1"), version="2.0.1",
        parent_dna=(DnaParent(root.dna_id, root.version, "sha256:wrong"),),
    )
    with pytest.raises(DnaError, match="missing or digest"):
        await service.register(wrong, correlation_id="wrong")
    child = dna("2.0.0", root)
    await service.register(child, correlation_id="child")
    async with database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO dna_parent VALUES (?,?,?,?,?,?)",
            (root.dna_id, root.version, 0, child.dna_id, child.version, child.content_digest),
        )
    with pytest.raises(DnaError, match="cycle"):
        await service.register(dna("3.0.0", root), correlation_id="cycle")
    too_many = DnaDefinition.from_workflow(
        workflow("4.0.0"), version="4.0.0",
        parent_dna=tuple(DnaParent(root.dna_id, root.version, root.content_digest)
                         for _ in range(5)),
    )
    with pytest.raises(DnaError, match="too many"):
        await service.register(too_many, correlation_id="many")
    await database.close()


@pytest.mark.asyncio
async def test_cas_state_machine_unique_active_and_append_only_audit(tmp_path: Path) -> None:
    database, service = await registry(tmp_path / "states.db")
    first = dna("1.0.0")
    await promote(service, first, "first")
    with pytest.raises(DnaError, match="revision conflict"):
        await service.transition(first.dna_id, first.version, DnaStatus.ACTIVE,
                                 expected_revision=2, reason="stale", correlation_id="first")
    active = await service.activate(first.dna_id, first.version, expected_revision=3,
                                    reason="launch", correlation_id="first")
    assert active.revision == 4
    history = await service.history(first.dna_id, first.version)
    assert [row["to_revision"] for row in history] == [0, 1, 2, 3, 4]
    assert len({row["event_id"] for row in history}) == 5
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_transition WHERE transition_id=?", (history[0]["transition_id"],)
            )

    second = dna("2.0.0", first)
    await promote(service, second, "second")
    activated = await service.activate(second.dna_id, second.version, expected_revision=3,
                                       reason="replace", correlation_id="second")
    assert activated.dna.status is DnaStatus.ACTIVE
    assert (await service.get(first.dna_id, first.version)).dna.status is DnaStatus.DEPRECATED
    rows = await database.fetch_all(
        "SELECT version FROM dna_definition WHERE dna_id=? AND status='ACTIVE'", (first.dna_id,)
    )
    assert [row["version"] for row in rows] == ["2.0.0"]
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as transaction:
            await transaction.execute(
                "UPDATE dna_definition SET status='ACTIVE' WHERE dna_id=? AND version='1.0.0'",
                (first.dna_id,),
            )
    await database.close()


@pytest.mark.asyncio
async def test_rollback_is_atomic_when_either_cas_check_fails(tmp_path: Path) -> None:
    database, service = await registry(tmp_path / "rollback.db")
    first, second = dna("1.0.0"), dna("2.0.0")
    await promote(service, first, "first")
    await service.activate(first.dna_id, first.version, expected_revision=3,
                           reason="launch", correlation_id="first")
    await promote(service, second, "second")
    await service.activate(second.dna_id, second.version, expected_revision=3,
                           reason="replace", correlation_id="second")
    with pytest.raises(DnaError, match="revision conflict"):
        await service.rollback(first.dna_id, first.version, expected_active_revision=4,
                               expected_target_revision=999, reason="bad rollback",
                               correlation_id="rollback")
    assert (await service.get(first.dna_id, first.version)).dna.status is DnaStatus.DEPRECATED
    assert (await service.get(second.dna_id, second.version)).dna.status is DnaStatus.ACTIVE
    restored = await service.rollback(
        first.dna_id, first.version, expected_active_revision=4, expected_target_revision=5,
        reason="regression", correlation_id="rollback",
    )
    assert restored.dna.status is DnaStatus.ACTIVE
    assert (await service.get(second.dna_id, second.version)).dna.status is DnaStatus.DEPRECATED
    await database.close()


@pytest.mark.asyncio
async def test_registry_rejects_invalid_entry_points_and_transition_shortcuts(
    tmp_path: Path,
) -> None:
    database, service = await registry(tmp_path / "guards.db")
    validated = DnaDefinition.from_workflow(
        workflow("1.0.0"), version="1.0.0", status=DnaStatus.VALIDATED
    )
    with pytest.raises(DnaError, match="must be CANDIDATE"):
        await service.register(validated, correlation_id="invalid")
    with pytest.raises(DnaError, match="not found"):
        await service.get("agent_market", "9.9.9")
    with pytest.raises(DnaError, match="not found"):
        await service.transition(
            "agent_market", "9.9.9", DnaStatus.VALIDATED,
            expected_revision=0, reason="missing", correlation_id="invalid",
        )
    candidate = dna("1.0.0")
    await service.register(candidate, correlation_id="candidate")
    with pytest.raises(DnaError, match="illegal DNA transition"):
        await service.transition(
            candidate.dna_id, candidate.version, DnaStatus.ACTIVE,
            expected_revision=0, reason="shortcut", correlation_id="invalid",
        )
    with pytest.raises(DnaError, match="only CANARY"):
        await service.activate(candidate.dna_id, candidate.version, expected_revision=0,
                               reason="shortcut", correlation_id="invalid")
    with pytest.raises(DnaError, match="requires an active"):
        await service.rollback(candidate.dna_id, candidate.version, expected_active_revision=0,
                               expected_target_revision=0, reason="nothing active",
                               correlation_id="invalid")
    self_parent = DnaDefinition.from_workflow(
        workflow("2.0.0"), version="2.0.0",
        parent_dna=(DnaParent("agent_market", "2.0.0", "sha256:self"),),
    )
    with pytest.raises(DnaError, match="cycle"):
        await service.register(self_parent, correlation_id="invalid")

    await service.transition(candidate.dna_id, candidate.version, DnaStatus.VALIDATED,
                             expected_revision=0, reason="valid", correlation_id="candidate")
    await service.transition(candidate.dna_id, candidate.version, DnaStatus.SHADOW,
                             expected_revision=1, reason="valid", correlation_id="candidate")
    await service.transition(candidate.dna_id, candidate.version, DnaStatus.CANARY,
                             expected_revision=2, reason="valid", correlation_id="candidate")
    await service.activate(candidate.dna_id, candidate.version, expected_revision=3,
                           reason="valid", correlation_id="candidate")
    target = dna("3.0.0")
    await service.register(target, correlation_id="target")
    with pytest.raises(DnaError, match="target must be DEPRECATED"):
        await service.rollback(candidate.dna_id, target.version, expected_active_revision=4,
                               expected_target_revision=0, reason="invalid target",
                               correlation_id="invalid")
    await database.close()
