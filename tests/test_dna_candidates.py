from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from test_experience_dataset import START, spec
from test_experience_dataset import setup as dataset_setup

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk import (
    CandidateMode,
    CandidateOperation,
    CandidateOperationKind,
    CandidatePolicy,
    CandidateRequest,
    DnaCandidateError,
    DnaCandidateGenerator,
    DnaDefinition,
    DnaStatus,
    ExperienceDataset,
    PersistentDnaRegistry,
)


def policy(**changes: object) -> CandidatePolicy:
    values: dict[str, object] = {
        "policy_version": "candidate-policy/1.0",
        "mutable_paths": frozenset({
            "workflow.nodes.*.input.*",
            "workflow.nodes.*.constraints.max_latency_ms",
            "workflow.nodes.*.constraints.freshness_seconds",
            "workflow.nodes.*.capability_version",
            "workflow.nodes",
            "workflow.nodes.*",
        }),
        "allowed_capabilities": frozenset({"market.summarize"}),
        "allowed_bindings": frozenset({("market.summarize", "1.0"),
                                       ("market.summarize", "2.0")}),
    }
    values.update(changes)
    return CandidatePolicy(**values)  # type: ignore[arg-type]


async def setup(tmp_path: Path) -> tuple[
    SQLiteDatabase, DnaDefinition, DnaDefinition, ExperienceDataset, DnaCandidateGenerator,
]:
    database, raw_base, raw_donor, builder = await dataset_setup(tmp_path)
    dataset = await builder.build(spec(raw_base, raw_donor))
    base, donor = raw_base.with_status(DnaStatus.VALIDATED), raw_donor.with_status(DnaStatus.VALIDATED)
    generator = DnaCandidateGenerator(
        database, FakeClock(START + timedelta(hours=4)), policy()
    )
    return database, base, donor, dataset, generator


def mutation(base: object, dataset: object, **changes: object) -> CandidateRequest:
    values: dict[str, object] = {
        "proposal_id": "proposal-mutation-1", "mode": CandidateMode.MUTATION,
        "base": base, "new_version": "3.0.0", "dataset": dataset,
        "hypothesis": "tighter latency improves stability",
        "operations": (
            CandidateOperation(CandidateOperationKind.SET_CONSTRAINT, "summary",
                               "max_latency_ms", 2500),
            CandidateOperation(CandidateOperationKind.SET_INPUT, "summary", "temperature", 0.1),
        ),
        "correlation_id": "candidate-correlation",
    }
    values.update(changes)
    return CandidateRequest(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mutation_creates_audited_candidate_and_registry_accepts_it(tmp_path: Path) -> None:
    database, base, _, dataset, generator = await setup(tmp_path)
    request = mutation(base, dataset)
    proposal = await generator.generate(request)
    assert proposal.candidate.status is DnaStatus.CANDIDATE
    assert proposal.candidate.version == "3.0.0"
    assert proposal.candidate.parent_dna[0].content_digest == base.content_digest
    assert proposal.candidate.workflow["nodes"][0]["constraints"]["max_latency_ms"] == 2500  # type: ignore[index]
    assert proposal.candidate.workflow["nodes"][0]["input"]["temperature"] == 0.1  # type: ignore[index]
    assert proposal.candidate.generator["dataset_manifest"] == dataset.manifest.manifest_digest
    assert await generator.generate(request) == proposal
    assert await generator.get(request.proposal_id) == proposal

    registry = PersistentDnaRegistry(
        database, FakeClock(START + timedelta(hours=4)),
        FakeUuidGenerator(UUID(int=value) for value in range(500, 520)),
    )
    registered = await registry.register(proposal.candidate, correlation_id="candidate")
    assert registered.dna.content_digest == proposal.candidate.content_digest
    await database.close()


@pytest.mark.asyncio
async def test_crossover_pins_two_parents_and_requires_dataset_evidence(tmp_path: Path) -> None:
    database, base, donor, dataset, generator = await setup(tmp_path)
    request = CandidateRequest(
        "proposal-cross-1", CandidateMode.CROSSOVER, base, "3.1.0", dataset,
        "reuse the donor summary node",
        (CandidateOperation(CandidateOperationKind.REPLACE_FROM_DONOR, "summary"),),
        "cross-correlation", donor,
    )
    proposal = await generator.generate(request)
    assert proposal.mode is CandidateMode.CROSSOVER
    assert [item.version for item in proposal.candidate.parent_dna] == ["1.0.0", "2.0.0"]
    assert proposal.candidate.generator["mode"] == "CROSSOVER"
    forged_dataset = replace(
        dataset, manifest=replace(dataset.manifest, manifest_digest="sha256:forged")
    )
    with pytest.raises(DnaCandidateError, match="manifest does not match"):
        await generator.generate(replace(request, proposal_id="proposal-cross-2",
                                         new_version="3.2.0", dataset=forged_dataset))
    await database.close()


@pytest.mark.asyncio
async def test_node_and_binding_mutations_remain_inside_governance(tmp_path: Path) -> None:
    database, base, _, dataset, _ = await setup(tmp_path)
    binding = DnaCandidateGenerator(database, FakeClock(START), policy()).generate
    proposal = await binding(mutation(
        base, dataset, proposal_id="proposal-binding", new_version="4.0.0",
        operations=(CandidateOperation(CandidateOperationKind.SET_CAPABILITY_VERSION,
                                       "summary", value="2.0"),),
    ))
    assert proposal.candidate.workflow["nodes"][0]["capability_version"] == "2.0"  # type: ignore[index]
    added = {
        "node_id": "verify", "type": "skill", "depends_on": ["summary"],
        "capability": "market.summarize", "capability_version": "1.0", "input": {},
        "constraints": {"side_effect": "PURE"},
    }
    proposal = await DnaCandidateGenerator(database, FakeClock(START), policy()).generate(
        mutation(base, dataset, proposal_id="proposal-add", new_version="4.1.0",
                 operations=(CandidateOperation(CandidateOperationKind.ADD_SKILL_NODE,
                                                "verify", value=added),))
    )
    assert len(proposal.candidate.workflow["nodes"]) == 2  # type: ignore[arg-type]
    denied = DnaCandidateGenerator(
        database, FakeClock(START), policy(allowed_bindings=frozenset({("market.summarize", "1.0")}))
    )
    with pytest.raises(DnaCandidateError, match="denied capability binding"):
        await denied.generate(mutation(
            base, dataset, proposal_id="proposal-denied", new_version="4.2.0",
            operations=(CandidateOperation(CandidateOperationKind.SET_CAPABILITY_VERSION,
                                           "summary", value="2.0"),),
        ))
    await database.close()


@pytest.mark.asyncio
async def test_mutable_paths_graph_validation_dedup_and_append_only_guards(tmp_path: Path) -> None:
    database, base, _, dataset, generator = await setup(tmp_path)
    with pytest.raises(DnaCandidateError, match="outside mutable_paths"):
        await DnaCandidateGenerator(
            database, FakeClock(START),
            policy(mutable_paths=frozenset({"workflow.nodes.*.input.allowed"})),
        ).generate(mutation(base, dataset))
    with pytest.raises(DnaCandidateError, match="operation limit"):
        await DnaCandidateGenerator(database, FakeClock(START), policy(max_operations=1)).generate(
            mutation(base, dataset)
        )
    with pytest.raises(DnaCandidateError, match="version must be new"):
        await generator.generate(mutation(base, dataset, new_version=base.version))
    with pytest.raises(DnaCandidateError, match="has not passed validation"):
        await generator.generate(mutation(base.with_status(DnaStatus.CANDIDATE), dataset))
    with pytest.raises(DnaCandidateError, match="node not found"):
        await generator.generate(mutation(
            base, dataset, proposal_id="proposal-missing-node", new_version="4.9.0",
            operations=(CandidateOperation(CandidateOperationKind.SET_INPUT,
                                           "missing", "value", 1),),
        ))
    with pytest.raises(DnaCandidateError, match="added node is invalid"):
        await generator.generate(mutation(
            base, dataset, proposal_id="proposal-invalid-add", new_version="4.9.1",
            operations=(CandidateOperation(CandidateOperationKind.ADD_SKILL_NODE,
                                           "new_node", value=None),),
        ))
    with pytest.raises(DnaCandidateError, match="workflow validation failed"):
        await generator.generate(mutation(
            base, dataset, proposal_id="proposal-remove", new_version="5.0.0",
            operations=(CandidateOperation(CandidateOperationKind.REMOVE_NODE, "summary"),),
        ))
    with pytest.raises(DnaCandidateError, match="constraint is invalid"):
        await generator.generate(mutation(
            base, dataset, proposal_id="proposal-bad-limit", new_version="5.1.0",
            operations=(CandidateOperation(CandidateOperationKind.SET_CONSTRAINT, "summary",
                                           "max_latency_ms", -1),),
        ))
    first = await generator.generate(mutation(base, dataset))
    with pytest.raises(DnaCandidateError, match="different content"):
        await generator.generate(mutation(base, dataset, hypothesis="different"))
    with pytest.raises(DnaCandidateError, match="already has a proposal"):
        await generator.generate(mutation(base, dataset, proposal_id="proposal-duplicate"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        async with database.transaction() as transaction:
            await transaction.execute(
                "DELETE FROM dna_candidate_proposal WHERE proposal_id=?", (first.proposal_id,)
            )
    with pytest.raises(DnaCandidateError, match="not found"):
        await generator.get("missing-proposal")
    await database.close()


def test_candidate_contracts_reject_unsafe_or_incomplete_configuration() -> None:
    with pytest.raises(DnaCandidateError, match="node_id"):
        CandidateOperation(CandidateOperationKind.SET_INPUT, "Bad-Node", "x", 1)
    with pytest.raises(DnaCandidateError, match="mutable paths"):
        CandidatePolicy("v1", frozenset(), frozenset(), frozenset())
    with pytest.raises(DnaCandidateError, match="immutable boundary"):
        CandidatePolicy("v1", frozenset({"workflow.policy.timeout_seconds"}),
                        frozenset(), frozenset())
    with pytest.raises(DnaCandidateError, match="max_side_effect"):
        policy(max_side_effect="UNSAFE")
    with pytest.raises(DnaCandidateError, match="positive"):
        policy(max_operations=0)


def test_candidate_request_mode_contracts_are_strict(tmp_path: Path) -> None:
    # Constructors are exercised with lightweight sentinels because validation fails before use.
    sentinel = object()
    with pytest.raises(DnaCandidateError, match="metadata"):
        mutation(sentinel, sentinel, proposal_id="x")
    with pytest.raises(DnaCandidateError, match="contain operations"):
        mutation(sentinel, sentinel, operations=())
    with pytest.raises(DnaCandidateError, match="requires a donor"):
        mutation(sentinel, sentinel, mode=CandidateMode.CROSSOVER)
    with pytest.raises(DnaCandidateError, match="donor replacement"):
        mutation(sentinel, sentinel, mode=CandidateMode.CROSSOVER, donor=sentinel)
    with pytest.raises(DnaCandidateError, match="cannot declare a donor"):
        mutation(sentinel, sentinel, donor=sentinel)
    with pytest.raises(DnaCandidateError, match="cannot use a donor"):
        mutation(
            sentinel, sentinel,
            operations=(CandidateOperation(CandidateOperationKind.REPLACE_FROM_DONOR, "node"),),
        )
