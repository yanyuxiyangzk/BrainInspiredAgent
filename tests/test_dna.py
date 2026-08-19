from __future__ import annotations

import pytest

from domain_sdk import DnaDefinition, DnaError, DnaParent, DnaRegistry, DnaStatus


def workflow(version: str = "1.0.0") -> dict[str, object]:
    return {
        "spec_version": "1.0", "workflow_id": "research_flow", "version": version,
        "name": "Research flow", "input_schema": {"type": "object"},
        "policy": {"timeout_seconds": 10, "max_parallelism": 1,
                    "required_capabilities": ["research.text.keywords"]},
        "nodes": [{"node_id": "keywords", "type": "skill", "depends_on": [],
                   "capability": "research.text.keywords", "capability_version": "1.0",
                   "input": {"text": "$.params.text"}, "constraints": {"side_effect": "PURE"}}],
        "output_mapping": {"keywords": "$.nodes.keywords.output.keywords"},
    }


def test_workflow_is_wrapped_as_immutable_dna_with_stable_digest() -> None:
    document = workflow()
    dna = DnaDefinition.from_workflow(document)
    assert dna.dna_id == "research_flow"
    assert dna.status is DnaStatus.CANDIDATE
    assert dna.digest.startswith("sha256:")
    assert dna.digest == dna.content_digest
    assert dna.envelope_digest.startswith("sha256:")
    assert dna.to_document()["workflow"] == document
    with pytest.raises(TypeError):
        dna.workflow["version"] = "2.0.0"  # type: ignore[index]


def test_content_digest_is_execution_identity_and_envelope_digest_tracks_metadata() -> None:
    first = DnaDefinition.from_workflow(
        workflow(), dna_id="agent.research", version="3.0.0",
        generator={"name": "rule", "version": "1.0"},
    )
    promoted = first.with_status(DnaStatus.VALIDATED)
    regenerated = DnaDefinition.from_workflow(
        workflow(), dna_id="agent.research", version="3.0.0",
        generator={"name": "model", "version": "2.0"},
    )
    assert first.content_digest == promoted.content_digest == regenerated.content_digest
    assert len({first.envelope_digest, promoted.envelope_digest,
                regenerated.envelope_digest}) == 3


def test_dna_registry_requires_explicit_validation_before_activation() -> None:
    registry = DnaRegistry()
    candidate = registry.register(DnaDefinition.from_workflow(workflow()))
    with pytest.raises(DnaError, match="only VALIDATED"):
        registry.activate(candidate.dna_id, candidate.version)
    validated = registry.register(DnaDefinition.from_workflow(
        workflow("1.1.0"), dna_id="agent.research", version="2.0.0",
        status=DnaStatus.VALIDATED,
        parent_dna=(DnaParent(candidate.dna_id, candidate.version, candidate.content_digest),),
        generator={"name": "test", "version": "1.0"},
    ))
    active = registry.activate(validated.dna_id, validated.version)
    assert active.status is DnaStatus.ACTIVE
    assert registry.active("agent.research").version == "2.0.0"
    with pytest.raises(DnaError, match="already registered"):
        registry.register(validated)


def test_new_active_version_deprecates_previous_active() -> None:
    registry = DnaRegistry()
    first = registry.register(DnaDefinition.from_workflow(workflow(), status=DnaStatus.VALIDATED))
    registry.activate(first.dna_id, first.version)
    second = registry.register(DnaDefinition.from_workflow(workflow("2.0.0"), status=DnaStatus.VALIDATED))
    registry.activate(second.dna_id, second.version)
    assert registry.get("research_flow", "1.0.0").status is DnaStatus.DEPRECATED
    assert registry.get("research_flow", "2.0.0").status is DnaStatus.ACTIVE


def test_dna_rejects_invalid_identity_and_missing_registry_entries() -> None:
    missing = workflow()
    missing.pop("workflow_id")
    with pytest.raises(DnaError, match="workflow_id"):
        DnaDefinition.from_workflow(missing)
    invalid_id = workflow()
    invalid_id["workflow_id"] = "X"
    with pytest.raises(DnaError, match="dna_id"):
        DnaDefinition.from_workflow(invalid_id)
    invalid_version = workflow()
    invalid_version["version"] = "one"
    with pytest.raises(DnaError, match="version"):
        DnaDefinition.from_workflow(invalid_version)
    registry = DnaRegistry()
    with pytest.raises(DnaError, match="not found"):
        registry.get("missing", "1.0.0")
    with pytest.raises(DnaError, match="no unique active"):
        registry.active("missing")
    assert registry.all() == ()
