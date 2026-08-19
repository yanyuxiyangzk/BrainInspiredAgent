from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from test_dna_candidates import START
from test_dna_candidates import setup as dna_setup

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from domain_sdk import (
    AgentDnaDefinition,
    AgentDnaError,
    AgentPolicyProfile,
    DnaStatus,
    PersistentAgentDnaRegistry,
    PersistentDnaRegistry,
    WorkflowDnaReference,
)


def profile(**changes: object) -> AgentPolicyProfile:
    values: dict[str, object] = {
        "goal": {"allowed_goal_types": ["market.summary", "daily.review"],
                 "max_active_goals": 3, "default_priority": 0.7},
        "attention": {"salience_weights": {"market_event": 1.0, "timer": 0.4},
                      "max_focus_items": 5, "switch_threshold": 0.6},
        "planning": {"strategy": "HYBRID", "horizon_seconds": 3600, "max_tasks": 8},
        "memory": {"working_items": 20, "episodic_retention_days": 30,
                   "semantic_candidates": 100},
        "evaluation": {"minimum_evidence_score": 0.8, "minimum_value_score": 0.7,
                       "review_interval_seconds": 86400},
    }
    values.update(changes)
    return AgentPolicyProfile(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_dna_references_workflow_and_has_independent_active_version(
    tmp_path: Path,
) -> None:
    database, base, _, _, _ = await dna_setup(tmp_path)
    clock = FakeClock(START)
    workflow_registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(3000, 3020)),
    )
    workflow = await workflow_registry.get(base.dna_id, base.version)
    workflow = await workflow_registry.transition(
        base.dna_id, base.version, DnaStatus.VALIDATED,
        expected_revision=workflow.revision, reason="Agent dependency validated",
        correlation_id="agent-correlation",
    )
    reference = WorkflowDnaReference(
        "market_summary", base.dna_id, base.version, base.content_digest,
    )
    agent = AgentDnaDefinition.create(
        "agent.market_research", "1.0.0", profile(), (reference,),
        generator={"name": "human", "version": "1.0"},
    )
    assert AgentDnaDefinition.from_document(agent.to_document()) == agent
    assert agent.content_digest != agent.envelope_digest
    registry = PersistentAgentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(4000, 4040)),
    )
    record = await registry.register(agent, correlation_id="agent-correlation")
    record = await registry.transition(
        agent.dna_id, agent.version, DnaStatus.VALIDATED,
        expected_revision=record.revision, reason="profile reviewed",
        correlation_id="agent-correlation",
    )
    active = await registry.transition(
        agent.dna_id, agent.version, DnaStatus.ACTIVE,
        expected_revision=record.revision, reason="agent activated",
        correlation_id="agent-correlation",
    )
    assert active.dna.status is DnaStatus.ACTIVE
    assert (await registry.active(agent.dna_id)) == active
    assert active.dna.profile.planning["strategy"] == "HYBRID"
    assert workflow.dna.status is DnaStatus.VALIDATED
    second = AgentDnaDefinition.create(
        agent.dna_id, "2.0.0", profile(planning={"strategy": "DELIBERATIVE",
                                                 "horizon_seconds": 7200,
                                                 "max_tasks": 10}), (reference,),
    )
    replacement = await registry.register(second, correlation_id="agent-correlation")
    replacement = await registry.transition(
        second.dna_id, second.version, DnaStatus.VALIDATED,
        expected_revision=replacement.revision, reason="replacement validated",
        correlation_id="agent-correlation",
    )
    replacement = await registry.transition(
        second.dna_id, second.version, DnaStatus.ACTIVE,
        expected_revision=replacement.revision, reason="replacement active",
        correlation_id="agent-correlation",
    )
    assert (await registry.get(agent.dna_id, agent.version)).dna.status is DnaStatus.DEPRECATED
    assert (await registry.active(agent.dna_id)) == replacement
    await database.close()


@pytest.mark.asyncio
async def test_agent_dna_rejects_unavailable_workflow_and_audits_transitions(
    tmp_path: Path,
) -> None:
    database, base, _, _, _ = await dna_setup(tmp_path)
    clock = FakeClock(START)
    registry = PersistentAgentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(5000, 5040)),
    )
    with pytest.raises(AgentDnaError, match="not found"):
        await registry.get("agent.missing", "1.0.0")
    with pytest.raises(AgentDnaError, match="no active"):
        await registry.active("agent.missing")
    with pytest.raises(AgentDnaError, match="not found"):
        await registry.transition(
            "agent.missing", "1.0.0", DnaStatus.VALIDATED, expected_revision=0,
            reason="missing", correlation_id="correlation",
        )
    agent = AgentDnaDefinition.create(
        "agent.market_research", "1.0.0", profile(),
        (WorkflowDnaReference("market_summary", base.dna_id, base.version,
                              base.content_digest),),
    )
    with pytest.raises(AgentDnaError, match="must be CANDIDATE"):
        await registry.register(agent.with_status(DnaStatus.VALIDATED),
                                correlation_id="correlation")
    with pytest.raises(AgentDnaError, match="unavailable Workflow"):
        await registry.register(agent, correlation_id="correlation")
    workflow_registry = PersistentDnaRegistry(
        database, clock, FakeUuidGenerator(UUID(int=value) for value in range(6000, 6020)),
    )
    workflow = await workflow_registry.get(base.dna_id, base.version)
    await workflow_registry.transition(
        base.dna_id, base.version, DnaStatus.VALIDATED,
        expected_revision=workflow.revision, reason="validated", correlation_id="correlation",
    )
    record = await registry.register(agent, correlation_id="correlation")
    with pytest.raises(AgentDnaError, match="revision conflict"):
        await registry.transition(
            agent.dna_id, agent.version, DnaStatus.VALIDATED, expected_revision=99,
            reason="conflict", correlation_id="correlation",
        )
    record = await registry.transition(
        agent.dna_id, agent.version, DnaStatus.VALIDATED,
        expected_revision=record.revision, reason="validated", correlation_id="correlation",
    )
    with pytest.raises(AgentDnaError, match="illegal"):
        await registry.transition(
            agent.dna_id, agent.version, DnaStatus.DEPRECATED,
            expected_revision=record.revision, reason="skip", correlation_id="correlation",
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute("DELETE FROM agent_dna_transition")
    await database.close()


def test_agent_dna_contracts_and_digest_boundaries() -> None:
    with pytest.raises(AgentDnaError, match="Workflow reference"):
        WorkflowDnaReference("!", "", "x", "bad")
    with pytest.raises(AgentDnaError, match="identity"):
        AgentDnaDefinition.create("!", "x", profile(), ())
    with pytest.raises(AgentDnaError, match="roles"):
        AgentDnaDefinition.create("agent.test", "1.0.0", profile(), ())
    with pytest.raises(AgentDnaError, match="policy fields"):
        profile(goal={})
    with pytest.raises(AgentDnaError, match="strategy"):
        profile(planning={"strategy": "FREE", "horizon_seconds": 1, "max_tasks": 1})
    with pytest.raises(AgentDnaError, match="goal types"):
        profile(goal={"allowed_goal_types": [], "max_active_goals": 1,
                      "default_priority": 0.5})
    with pytest.raises(AgentDnaError, match="positive integers"):
        profile(memory={"working_items": 0, "episodic_retention_days": 1,
                        "semantic_candidates": 1})
    with pytest.raises(AgentDnaError, match="scores"):
        profile(evaluation={"minimum_evidence_score": 2,
                            "minimum_value_score": 0.5, "review_interval_seconds": 1})
    with pytest.raises(AgentDnaError, match="salience weights"):
        profile(attention={"salience_weights": {}, "max_focus_items": 1,
                           "switch_threshold": 0.5})
    reference = WorkflowDnaReference("market_summary", "flow", "1.0.0", "sha256:x")
    first = AgentDnaDefinition.create("agent.test", "1.0.0", profile(), (reference,))
    changed = AgentDnaDefinition.create(
        "agent.test", "1.0.0", profile(planning={"strategy": "REACTIVE",
                                                  "horizon_seconds": 60,
                                                  "max_tasks": 2}), (reference,),
    )
    assert first.content_digest != changed.content_digest
    assert first.with_status(DnaStatus.VALIDATED).content_digest == first.content_digest
    document = first.to_document()
    with pytest.raises(AgentDnaError, match="document is invalid"):
        AgentDnaDefinition.from_document({})
    with pytest.raises(AgentDnaError, match="kind or version"):
        AgentDnaDefinition.from_document(document | {"kind": "WORKFLOW"})
    with pytest.raises(AgentDnaError, match="content digest"):
        AgentDnaDefinition.from_document(document | {"content_digest": "sha256:bad"})
    with pytest.raises(AgentDnaError, match="envelope digest"):
        AgentDnaDefinition.from_document(document | {"envelope_digest": "sha256:bad"})
