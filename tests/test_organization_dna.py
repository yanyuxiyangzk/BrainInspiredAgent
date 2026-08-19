from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from test_agent_dna import profile as agent_profile
from test_dna_candidates import START
from test_dna_candidates import setup as dna_setup

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from domain_sdk import (
    AgentDnaDefinition,
    DnaStatus,
    OrganizationDnaDefinition,
    OrganizationDnaError,
    OrganizationMember,
    OrganizationPolicyProfile,
    PersistentAgentDnaRegistry,
    PersistentDnaRegistry,
    PersistentOrganizationDnaRegistry,
    WorkflowDnaReference,
)


def profile(**changes: object) -> OrganizationPolicyProfile:
    values: dict[str, object] = {
        "communication": {"channels": ["task", "evidence"],
                          "max_message_bytes": 65536, "max_hops": 4},
        "delegation": {"strategy": "RESPONSIBILITY", "max_inflight_per_agent": 2},
        "arbitration": {"strategy": "QUORUM", "quorum_ratio": 0.5,
                        "tie_break_role": "lead"},
        "budget": {"max_tokens": 10000, "max_cost_minor": 1000,
                   "max_duration_seconds": 3600, "max_parallel_agents": 2},
        "failure": {"max_member_failures": 2, "isolation_seconds": 300,
                    "fallback_role": "lead"},
    }
    values.update(changes)
    return OrganizationPolicyProfile(**values)  # type: ignore[arg-type]


async def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    database, workflow, _, _, _ = await dna_setup(tmp_path)
    clock = FakeClock(START)
    workflow_registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(7000, 7020)),
    )
    workflow_record = await workflow_registry.get(workflow.dna_id, workflow.version)
    await workflow_registry.transition(
        workflow.dna_id, workflow.version, DnaStatus.VALIDATED,
        expected_revision=workflow_record.revision, reason="validated",
        correlation_id="organization-correlation",
    )
    agent = AgentDnaDefinition.create(
        "agent.market_team", "1.0.0", agent_profile(),
        (WorkflowDnaReference("market_summary", workflow.dna_id, workflow.version,
                              workflow.content_digest),),
    )
    agent_registry = PersistentAgentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(7100, 7140)),
    )
    agent_record = await agent_registry.register(agent, correlation_id="organization-correlation")
    agent_record = await agent_registry.transition(
        agent.dna_id, agent.version, DnaStatus.VALIDATED,
        expected_revision=agent_record.revision, reason="validated",
        correlation_id="organization-correlation",
    )
    members = (
        OrganizationMember("lead", agent.dna_id, agent.version, agent.content_digest,
                           ("synthesize", "research"), 100),
        OrganizationMember("researcher", agent.dna_id, agent.version, agent.content_digest,
                           ("research",), 80),
    )
    organization = OrganizationDnaDefinition.create(
        "org.market_research", "1.0.0", profile(), members,
        generator={"name": "human", "version": "1.0"},
    )
    registry = PersistentOrganizationDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(7200, 7260)),
    )
    return database, agent_record, organization, registry


@pytest.mark.asyncio
async def test_organization_dna_routes_arbitrates_budgets_and_activates(tmp_path: Path) -> None:
    database, _, organization, registry = await setup(tmp_path)
    assert OrganizationDnaDefinition.from_document(organization.to_document()) == organization
    assert organization.delegate("research").role == "lead"
    assert organization.delegate("unknown").role == "lead"
    assert organization.delegate("research", unavailable_roles=frozenset({"lead"})).role \
        == "researcher"
    with pytest.raises(OrganizationDnaError, match="no available delegate"):
        organization.delegate("unknown", unavailable_roles=frozenset({"lead"}))
    assert organization.arbitrate({"lead": True, "researcher": False})
    priority_org = OrganizationDnaDefinition.create(
        "org.priority", "1.0.0",
        profile(arbitration={"strategy": "PRIORITY", "quorum_ratio": 0.5,
                             "tie_break_role": "lead"}), organization.members,
    )
    assert priority_org.arbitrate({"lead": True})
    with pytest.raises(OrganizationDnaError, match="arbiter did not vote"):
        priority_org.arbitrate({"researcher": True})
    unanimous_org = OrganizationDnaDefinition.create(
        "org.unanimous", "1.0.0",
        profile(arbitration={"strategy": "UNANIMOUS", "quorum_ratio": 1.0,
                             "tie_break_role": "lead"}), organization.members,
    )
    assert unanimous_org.arbitrate({"lead": True, "researcher": True})
    organization.approve_budget(tokens=100, cost_minor=10, duration_seconds=60,
                                parallel_agents=2)
    with pytest.raises(OrganizationDnaError, match="budget exceeded"):
        organization.approve_budget(tokens=10001, cost_minor=10, duration_seconds=60,
                                    parallel_agents=2)
    record = await registry.register(organization, correlation_id="organization-correlation")
    record = await registry.transition(
        organization.dna_id, organization.version, DnaStatus.VALIDATED,
        expected_revision=record.revision, reason="validated",
        correlation_id="organization-correlation",
    )
    record = await registry.transition(
        organization.dna_id, organization.version, DnaStatus.ACTIVE,
        expected_revision=record.revision, reason="active",
        correlation_id="organization-correlation",
    )
    assert (await registry.active(organization.dna_id)) == record
    assert record.dna.content_digest == organization.content_digest
    second = OrganizationDnaDefinition.create(
        organization.dna_id, "2.0.0", profile(), organization.members,
    )
    replacement = await registry.register(second, correlation_id="organization-correlation")
    replacement = await registry.transition(
        second.dna_id, second.version, DnaStatus.VALIDATED,
        expected_revision=replacement.revision, reason="replacement validated",
        correlation_id="organization-correlation",
    )
    replacement = await registry.transition(
        second.dna_id, second.version, DnaStatus.ACTIVE,
        expected_revision=replacement.revision, reason="replacement active",
        correlation_id="organization-correlation",
    )
    assert (await registry.get(organization.dna_id,
                               organization.version)).dna.status is DnaStatus.DEPRECATED
    assert (await registry.active(organization.dna_id)) == replacement
    await database.close()


@pytest.mark.asyncio
async def test_organization_rejects_candidate_agent_and_audits(tmp_path: Path) -> None:
    database, agent, organization, registry = await setup(tmp_path)
    with pytest.raises(OrganizationDnaError, match="not found"):
        await registry.get("org.missing", "1.0.0")
    with pytest.raises(OrganizationDnaError, match="no active"):
        await registry.active("org.missing")
    with pytest.raises(OrganizationDnaError, match="not found"):
        await registry.transition(
            "org.missing", "1.0.0", DnaStatus.VALIDATED, expected_revision=0,
            reason="missing", correlation_id="correlation",
        )
    with pytest.raises(OrganizationDnaError, match="must be CANDIDATE"):
        await registry.register(organization.with_status(DnaStatus.VALIDATED),
                                correlation_id="correlation")
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='CANDIDATE' WHERE dna_id=? AND version=?",
            (agent.dna.dna_id, agent.dna.version),
        )
    with pytest.raises(OrganizationDnaError, match="unavailable Agent"):
        await registry.register(organization, correlation_id="correlation")
    async with database.transaction() as transaction:
        await transaction.execute(
            "UPDATE agent_dna_definition SET status='VALIDATED' WHERE dna_id=? AND version=?",
            (agent.dna.dna_id, agent.dna.version),
        )
    record = await registry.register(organization, correlation_id="correlation")
    with pytest.raises(OrganizationDnaError, match="revision conflict"):
        await registry.transition(
            organization.dna_id, organization.version, DnaStatus.VALIDATED,
            expected_revision=99, reason="conflict", correlation_id="correlation",
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute("DELETE FROM organization_dna_transition")
    assert record.revision == 0
    await database.close()


def test_organization_contracts() -> None:
    member = OrganizationMember("lead", "agent.lead", "1.0.0", "sha256:x",
                                ("research",), 10)
    second = OrganizationMember("researcher", "agent.researcher", "1.0.0", "sha256:y",
                                ("research",), 5)
    with pytest.raises(OrganizationDnaError, match="member is invalid"):
        OrganizationMember("!", "", "x", "bad", (), -1)
    with pytest.raises(OrganizationDnaError, match="multi-Agent"):
        OrganizationDnaDefinition.create("org.test", "1.0.0", profile(), (member,))
    with pytest.raises(OrganizationDnaError, match="identity"):
        OrganizationDnaDefinition.create("!", "x", profile(), (member, second))
    with pytest.raises(OrganizationDnaError, match="unknown role"):
        OrganizationDnaDefinition.create(
            "org.test", "1.0.0",
            profile(failure={"max_member_failures": 1, "isolation_seconds": 1,
                             "fallback_role": "missing"}), (member, second),
        )
    organization = OrganizationDnaDefinition.create(
        "org.test", "1.0.0", profile(), (member, second),
    )
    with pytest.raises(OrganizationDnaError, match="unknown or empty votes"):
        organization.arbitrate({})
    with pytest.raises(OrganizationDnaError, match="negative"):
        organization.approve_budget(tokens=-1, cost_minor=0, duration_seconds=0,
                                    parallel_agents=0)
    assert organization.with_status(DnaStatus.VALIDATED).content_digest \
        == organization.content_digest
    with pytest.raises(OrganizationDnaError, match="policy fields"):
        profile(communication={})
    with pytest.raises(OrganizationDnaError, match="strategy"):
        profile(delegation={"strategy": "FREE", "max_inflight_per_agent": 1})
    with pytest.raises(OrganizationDnaError, match="channels"):
        profile(communication={"channels": [], "max_message_bytes": 1, "max_hops": 1})
    with pytest.raises(OrganizationDnaError, match="positive integers"):
        profile(budget={"max_tokens": 0, "max_cost_minor": 1,
                        "max_duration_seconds": 1, "max_parallel_agents": 1})
    with pytest.raises(OrganizationDnaError, match="quorum ratio"):
        profile(arbitration={"strategy": "QUORUM", "quorum_ratio": 0,
                             "tie_break_role": "lead"})
    document = organization.to_document()
    with pytest.raises(OrganizationDnaError, match="document is invalid"):
        OrganizationDnaDefinition.from_document({})
    with pytest.raises(OrganizationDnaError, match="kind or version"):
        OrganizationDnaDefinition.from_document(document | {"kind": "AGENT"})
    with pytest.raises(OrganizationDnaError, match="content digest"):
        OrganizationDnaDefinition.from_document(document | {"content_digest": "sha256:bad"})
    with pytest.raises(OrganizationDnaError, match="envelope digest"):
        OrganizationDnaDefinition.from_document(document | {"envelope_digest": "sha256:bad"})
