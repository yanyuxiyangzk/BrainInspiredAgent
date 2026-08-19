from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from test_dna_candidates import START
from test_organization_dna import setup

from active_agent_platform.dna_execution import (
    DnaExecutionError,
    DnaExecutionIdentity,
    DnaIdentity,
    _content_digest,
    verify_execution_identity,
)
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from domain_sdk import (
    DnaStatus,
    OrganizationExecutionRequest,
    OrganizationGovernedApp,
    PersistentAgentDnaRegistry,
    PersistentDnaRegistry,
)


class GovernedStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, cycle: object, state: object, bindings: object,
                      **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return "executed"


@pytest.mark.asyncio
async def test_organization_entry_freezes_and_verifies_three_level_identity(
    tmp_path: Path,
) -> None:
    database, agent_record, organization, organizations = await setup(tmp_path)
    clock = FakeClock(START)
    agents = PersistentAgentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(8000, 8040)),
    )
    agent_record = await agents.transition(
        agent_record.dna.dna_id, agent_record.dna.version, DnaStatus.ACTIVE,
        expected_revision=agent_record.revision, reason="execution active", correlation_id="corr",
    )
    reference = agent_record.dna.workflow_dna[0]
    workflows = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(8100, 8140)),
    )
    workflow_record = await workflows.get(reference.dna_id, reference.version)
    for status in (DnaStatus.SHADOW, DnaStatus.CANARY):
        workflow_record = await workflows.transition(
            reference.dna_id, reference.version, status,
            expected_revision=workflow_record.revision, reason="execution promotion",
            correlation_id="corr",
        )
    workflow_record = await workflows.activate(
        reference.dna_id, reference.version, expected_revision=workflow_record.revision,
        reason="execution active", correlation_id="corr",
    )
    org_record = await organizations.register(organization, correlation_id="corr")
    org_record = await organizations.transition(
        organization.dna_id, organization.version, DnaStatus.VALIDATED,
        expected_revision=org_record.revision, reason="validated", correlation_id="corr",
    )
    await organizations.transition(
        organization.dna_id, organization.version, DnaStatus.ACTIVE,
        expected_revision=org_record.revision, reason="active", correlation_id="corr",
    )
    stub = GovernedStub()
    app = OrganizationGovernedApp(cast(Any, stub), organizations, agents, workflows)
    request = OrganizationExecutionRequest(
        organization.dna_id, "research", "market_summary", cast(Any, object()),
        cast(Any, object()), {}, 100, 10, 60, frozenset({"lead"}),
    )
    result = await app.execute(request)
    assert result.execution == "executed"
    assert result.identity.organization_role == "researcher"
    assert stub.calls[0]["dna_identity"] == result.identity
    assert stub.calls[0]["responsibility"] == "research"
    workflow = workflow_record.dna.workflow_validation
    await verify_execution_identity(
        database, result.identity, workflow_id=workflow.workflow_id,
        workflow_version=workflow.version, workflow_digest=workflow.digest,
    )
    forged = DnaExecutionIdentity(
        result.identity.organization, result.identity.organization_role,
        DnaIdentity(result.identity.agent.dna_id, result.identity.agent.version, "sha256:" + "0" * 64),
        result.identity.workflow,
    )
    with pytest.raises(DnaExecutionError, match="digest mismatch"):
        await verify_execution_identity(
            database, forged, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest=workflow.digest,
        )
    missing = DnaExecutionIdentity(
        DnaIdentity("org.missing", "1.0.0", result.identity.organization.content_digest),
        result.identity.organization_role, result.identity.agent, result.identity.workflow,
    )
    with pytest.raises(DnaExecutionError, match="missing durable"):
        await verify_execution_identity(
            database, missing, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest=workflow.digest,
        )
    async with database.transaction() as transaction:
        deprecated_agent = agent_record.dna.with_status(DnaStatus.DEPRECATED)
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='DEPRECATED',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(deprecated_agent.to_document()), deprecated_agent.envelope_digest,
             result.identity.agent.dna_id, result.identity.agent.version),
        )
    with pytest.raises(DnaExecutionError, match="active versions"):
        await verify_execution_identity(
            database, result.identity, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest=workflow.digest,
        )
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='ACTIVE',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(agent_record.dna.to_document()), agent_record.dna.envelope_digest,
             result.identity.agent.dna_id, result.identity.agent.version),
        )
    with pytest.raises(DnaExecutionError, match="planned Workflow"):
        await verify_execution_identity(
            database, result.identity, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest="sha256:" + "f" * 64,
        )
    bad_role = OrganizationExecutionRequest(
        organization.dna_id, "research", "missing", cast(Any, object()),
        cast(Any, object()), {}, 100, 10, 60,
    )
    with pytest.raises(DnaExecutionError, match="no unique Workflow role"):
        await app.execute(bad_role)
    wrong_role = DnaExecutionIdentity(
        result.identity.organization, "missing_role", result.identity.agent,
        result.identity.workflow,
    )
    with pytest.raises(DnaExecutionError, match="Organization role"):
        await verify_execution_identity(
            database, wrong_role, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest=workflow.digest,
        )
    async with database.transaction() as transaction:
        deprecated_agent = agent_record.dna.with_status(DnaStatus.DEPRECATED)
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='DEPRECATED',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(deprecated_agent.to_document()), deprecated_agent.envelope_digest,
             result.identity.agent.dna_id, result.identity.agent.version),
        )
    with pytest.raises(DnaExecutionError, match="delegated Agent"):
        await app.execute(request)
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='ACTIVE',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(agent_record.dna.to_document()), agent_record.dna.envelope_digest,
             result.identity.agent.dna_id, result.identity.agent.version),
        )
        deprecated_workflow = workflow_record.dna.with_status(DnaStatus.DEPRECATED)
        await transaction.execute(
            "UPDATE dna_definition SET status='DEPRECATED',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(deprecated_workflow.to_document()), deprecated_workflow.envelope_digest,
             result.identity.workflow.dna_id, result.identity.workflow.version),
        )
    with pytest.raises(DnaExecutionError, match="delegated Workflow"):
        await app.execute(request)
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE dna_definition SET status='ACTIVE',document_json=?,envelope_digest=? "
            "WHERE dna_id=? AND version=?",
            (json.dumps(workflow_record.dna.to_document()), workflow_record.dna.envelope_digest,
             result.identity.workflow.dna_id, result.identity.workflow.version),
        )
        row = await transaction.fetch_one(
            "SELECT document_json FROM organization_dna_definition WHERE dna_id=? AND version=?",
            (result.identity.organization.dna_id, result.identity.organization.version),
        )
        assert row is not None
        corrupted = json.loads(str(row["document_json"]))
        corrupted["members"][0]["priority"] = 1
        await transaction.execute(
            "UPDATE organization_dna_definition SET document_json=? WHERE dna_id=? AND version=?",
            (json.dumps(corrupted), result.identity.organization.dna_id,
             result.identity.organization.version),
        )
    with pytest.raises(DnaExecutionError, match="document digest"):
        await verify_execution_identity(
            database, result.identity, workflow_id=workflow.workflow_id,
            workflow_version=workflow.version, workflow_digest=workflow.digest,
        )
    assert len(result.identity.digest) == 71
    await database.close()


@pytest.mark.asyncio
async def test_execution_context_migration_is_append_only(tmp_path: Path) -> None:
    database, *_ = await setup(tmp_path)
    triggers = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'dna_execution_context%'",
    )
    assert {str(row["name"]) for row in triggers} == {
        "dna_execution_context_no_update", "dna_execution_context_no_delete",
    }
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as transaction:
            await transaction.execute(
                "INSERT INTO dna_execution_context VALUES (" + ",".join("?" for _ in range(20)) + ")",
                tuple("missing" for _ in range(20)),
            )
    await database.close()


def test_identity_and_request_contracts() -> None:
    with pytest.raises(DnaExecutionError, match="identity"):
        DnaIdentity("", "v1", "bad")
    identity = DnaIdentity("org.example", "1.0.0", "sha256:" + "a" * 64)
    with pytest.raises(DnaExecutionError, match="role"):
        DnaExecutionIdentity(identity, "!", identity, identity)
    with pytest.raises(DnaExecutionError, match="incomplete"):
        OrganizationExecutionRequest("", "", "", cast(Any, object()), cast(Any, object()),
                                     {}, 0, 0, 0)
    with pytest.raises(DnaExecutionError, match="kind"):
        _content_digest({"kind": "UNKNOWN"})
