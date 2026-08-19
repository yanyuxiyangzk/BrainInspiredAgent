from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    RefResolver,
)
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from apps.quant_agent import MARKET_SUMMARY_WORKFLOW, fake_skill_manifests

ROOT = Path(__file__).parents[1] / "schemas"
UUID = "00000000-0000-0000-0000-000000000001"
STAMP = "2026-08-18T08:00:00Z"
DIGEST = "sha256:" + "a" * 64


def _documents() -> dict[str, dict[str, object]]:
    return {
        str(path.relative_to(ROOT)): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(ROOT.glob("**/*.json"))
    }


def _binding() -> dict[str, object]:
    return {"schema_version": "1.0", "node_id": "read", "capability": "market.read",
            "capability_version": "1.0", "skill_id": "fake-read", "skill_version": "1.0.0",
            "skill_digest": DIGEST, "binding_policy_version": "policy/1", "resolved_at": STAMP}


def _error() -> dict[str, object]:
    return {"schema_version": "1.0", "error_id": UUID, "code": "INPUT_INVALID",
            "category": "VALIDATION", "message": "invalid", "retryable": False,
            "severity": "ERROR", "occurred_at": STAMP, "source": "test", "correlation_id": UUID}


def _plan() -> dict[str, object]:
    return {"schema_version": "1.0", "plan_id": UUID, "status": "CANDIDATE",
            "created_at": STAMP, "expires_at": "2026-08-18T08:05:00Z", "correlation_id": UUID,
            "trigger": {"type": "SCHEDULE", "source_id": "timer", "occurred_at": STAMP},
            "goal": {"goal_id": "daily.review", "priority": 60}, "reason": "review", "evidence": [],
            "tasks": [{"task_id": "00000000-0000-0000-0000-000000000002",
                       "workflow_id": "daily_review", "workflow_version": "1.0.0", "params": {},
                       "priority": 60, "deadline": "2026-08-18T08:04:00Z", "idempotency_key": "daily",
                       "depends_on": []}],
            "requested_budget": {"max_tokens": 1, "max_cost_minor": 0, "currency": "CNY",
                                 "max_duration_seconds": 60},
            "policy_context": {"brain_mode": "REVIEW", "market_phase": "CLOSED",
                               "data_fresh_until": "2026-08-18T08:04:00Z"}}


def _dna_context() -> dict[str, object]:
    identity = {"dna_id": "dna.example", "version": "1.0.0", "content_digest": DIGEST}
    return {"organization": identity, "organization_role": "researcher",
            "agent": identity, "workflow": identity, "context_digest": DIGEST,
            "responsibility": "research"}


def examples() -> dict[str, dict[str, object]]:
    binding = _binding()
    return {
        "dna/dna-execution-context-1.0.schema.json": _dna_context(),
        "dna/agent-dna-1.0.schema.json": {
            "dna_spec_version": "1.0", "dna_id": "agent.market_research",
            "version": "1.0.0", "kind": "AGENT", "status": "CANDIDATE",
            "content_digest": DIGEST, "envelope_digest": DIGEST,
            "profile": {
                "goal": {"allowed_goal_types": ["market.summary"], "max_active_goals": 3,
                         "default_priority": 0.7},
                "attention": {"salience_weights": {"market_event": 1.0},
                              "max_focus_items": 5, "switch_threshold": 0.6},
                "planning": {"strategy": "HYBRID", "horizon_seconds": 3600,
                             "max_tasks": 8},
                "memory": {"working_items": 20, "episodic_retention_days": 30,
                           "semantic_candidates": 100},
                "evaluation": {"minimum_evidence_score": 0.8,
                               "minimum_value_score": 0.7,
                               "review_interval_seconds": 86400},
            },
            "workflow_dna": [{"role": "market_summary", "dna_id": "market.summary",
                              "version": "1.0.0", "content_digest": DIGEST}],
            "generator": {"name": "human", "version": "1.0"},
        },
        "dna/organization-dna-1.0.schema.json": {
            "dna_spec_version": "1.0", "dna_id": "org.market_research",
            "version": "1.0.0", "kind": "ORGANIZATION", "status": "CANDIDATE",
            "content_digest": DIGEST, "envelope_digest": DIGEST,
            "profile": {
                "communication": {"channels": ["task", "evidence"],
                                  "max_message_bytes": 65536, "max_hops": 4},
                "delegation": {"strategy": "RESPONSIBILITY", "max_inflight_per_agent": 2},
                "arbitration": {"strategy": "QUORUM", "quorum_ratio": 0.5,
                                "tie_break_role": "lead"},
                "budget": {"max_tokens": 10000, "max_cost_minor": 1000,
                           "max_duration_seconds": 3600, "max_parallel_agents": 2},
                "failure": {"max_member_failures": 2, "isolation_seconds": 300,
                            "fallback_role": "lead"},
            },
            "members": [
                {"role": "lead", "agent_dna_id": "agent.lead", "agent_version": "1.0.0",
                 "agent_content_digest": DIGEST, "responsibilities": ["synthesize"],
                 "priority": 100},
                {"role": "researcher", "agent_dna_id": "agent.researcher",
                 "agent_version": "1.0.0", "agent_content_digest": DIGEST,
                 "responsibilities": ["research"], "priority": 80}
            ],
            "generator": {"name": "human", "version": "1.0"},
        },
        "capability/capability-contract-1.0.schema.json": {
            "schema_version": "1.0", "capability": "market.read", "version": "1.0",
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "side_effect": "PURE"},
        "dna/dna-1.0.schema.json": {
            "dna_spec_version": "1.0", "dna_id": "research.market_summary", "version": "1.0.0",
            "kind": "WORKFLOW", "status": "CANDIDATE", "content_digest": DIGEST,
            "envelope_digest": DIGEST, "workflow": copy.deepcopy(MARKET_SUMMARY_WORKFLOW),
            "parent_dna": [], "generator": {"name": "human", "version": "1.0"}},
        "error/error-1.0.schema.json": _error(),
        "event/core-event-payload-1.0.schema.json": {
            "event_type": "attention.salient_event", "stimulus_id": UUID, "data": {}, "data_quality": "VALID"},
        "event/event-envelope-1.0.schema.json": {
            "schema_version": "1.0", "msg_id": UUID, "msg_type": "attention.salient_event",
            "source": "test", "occurred_at": STAMP, "published_at": STAMP, "priority": 1,
            "correlation_id": UUID, "dedup_key": "key", "payload": {}},
        "evolution/workflow-patch-1.0.schema.json": {
            "schema_version": "1.0", "proposal_id": UUID,
            "base": {"workflow_id": "flow", "version": "1.0.0", "digest": DIGEST},
            "source": "HUMAN", "hypothesis": "improve", "operations": [{"op": "remove_node", "node_id": "n"}],
            "required_evidence": [], "requested_capabilities": []},
        "execution/execution-grant-1.0.schema.json": {
            "schema_version": "1.0", "grant_id": UUID, "decision_id": UUID, "plan_id": UUID,
            "task_id": UUID, "workflow": {"workflow_id": "flow", "version": "1.0.0", "digest": DIGEST},
            "bindings": [binding], "policy_version": "p", "world_snapshot_id": "w", "memory_snapshot_id": "m",
            "allowed_permissions": [], "budget": {"max_duration_seconds": 1, "max_tokens": 0,
            "max_cost_minor": 0, "currency": "CNY"}, "issued_at": STAMP,
            "expires_at": "2026-08-18T08:01:00Z", "consumption": "SINGLE_TASK_MULTI_ATTEMPT", "correlation_id": UUID},
        "plan/plan-1.0.schema.json": _plan(),
        "plan/plan-decision-1.0.schema.json": {
            "schema_version": "1.0", "decision_id": UUID, "plan_id": UUID, "decision": "APPROVED",
            "decided_at": STAMP, "validator_version": "1", "policy_version": "p",
            "world_snapshot_id": "w", "reasons": ["ok"], "correlation_id": UUID},
        "skill/skill-binding-1.0.schema.json": binding,
        "skill/skill-invocation-1.0.schema.json": {
            "schema_version": "1.0", "invocation_id": UUID, "task_id": UUID, "run_id": UUID,
            "node_id": "read", "binding": binding, "capability": "market.read", "capability_version": "1.0",
            "input": {}, "input_digest": DIGEST, "deadline": STAMP, "idempotency_key": "key",
            "attempt": 1, "allowed_permissions": [], "budget": {}, "correlation_id": UUID},
        "skill/skill-manifest-1.0.schema.json": fake_skill_manifests()[0],
        "skill/skill-result-1.0.schema.json": {
            "schema_version": "1.0", "invocation_id": UUID, "status": "SUCCEEDED", "output": {},
            "output_digest": DIGEST, "started_at": STAMP, "finished_at": STAMP, "usage": {}},
        "task/task-1.0.schema.json": {
            "schema_version": "1.0", "task_id": UUID, "plan_id": UUID, "grant_id": UUID,
            "correlation_id": UUID, "status": "SUCCEEDED", "workflow": {"workflow_id": "flow",
            "version": "1.0.0", "definition_digest": DIGEST}, "priority": 1, "deadline": STAMP,
            "idempotency_key": "key", "attempt": 1, "created_at": STAMP, "updated_at": STAMP,
            "input_digest": DIGEST, "node_summary": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}},
        "workflow/workflow-1.0.schema.json": copy.deepcopy(MARKET_SUMMARY_WORKFLOW),
        "workflow/workflow-node-1.0.schema.json": copy.deepcopy(
            cast(list[dict[str, object]], MARKET_SUMMARY_WORKFLOW["nodes"])[0]
        ),
    }


def _validator(schema: Mapping[str, object], all_schemas: Mapping[str, Mapping[str, object]]) -> Draft202012Validator:
    store = {str(value["$id"]): value for value in all_schemas.values()}
    return Draft202012Validator(schema, resolver=RefResolver.from_schema(schema, store=store), format_checker=FormatChecker())


def test_every_schema_is_draft_2020_12_compilable_and_versioned() -> None:
    documents = _documents()
    assert set(documents) == set(examples())
    for relative, schema in documents.items():
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert str(schema["$id"]).endswith(relative)


@pytest.mark.parametrize("relative", sorted(examples()))
def test_schema_positive_and_structural_negative_contracts(relative: str) -> None:
    documents, sample = _documents(), examples()[relative]
    validator = _validator(documents[relative], documents)
    validator.validate(sample)
    rejected = copy.deepcopy(sample)
    rejected["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(rejected)
    if "schema_version" in sample:
        wrong_version = copy.deepcopy(sample)
        wrong_version["schema_version"] = "9.0"
        with pytest.raises(ValidationError):
            validator.validate(wrong_version)
    required = documents[relative].get("required", [])
    if isinstance(required, list) and required:
        missing = copy.deepcopy(sample)
        del missing[str(required[0])]
        with pytest.raises(ValidationError):
            validator.validate(missing)
