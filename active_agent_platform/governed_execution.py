"""Domain-neutral cognition-to-outcome governed execution facade."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from active_agent_platform.coordinator import CognitiveCycle
from active_agent_platform.motor import MotorExec, MotorExecutionRequest, TaskStatus
from active_agent_platform.outcomes import OutcomeEvaluation, OutcomeEvaluator, OutcomeRequest
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.planning import GrantIssuer, PlanDecision, PlanningRepository
from active_agent_platform.prefrontal import PlannerResult, RulePlanner
from active_agent_platform.risk import RiskGate
from active_agent_platform.skills import SkillBinding
from active_agent_platform.state import BrainState
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow_runtime import WorkflowExecutionResult
from brain_kernel.ports import Clock, UuidGenerator


@dataclass(frozen=True, slots=True)
class GovernedExecutionResult:
    planner: PlannerResult
    decision_id: str
    grant_id: str
    execution: WorkflowExecutionResult
    outcome: OutcomeEvaluation


class GovernedCognitiveApp:
    """Execute any single-task cognitive plan through validation, grant and outcome."""

    def __init__(self, database: SQLiteDatabase, planner: RulePlanner, validator: PlanValidator,
                 risk_gate: RiskGate, motor: MotorExec, evaluator: OutcomeEvaluator,
                 clock: Clock, identifiers: UuidGenerator) -> None:
        self.database, self.planner, self.validator = database, planner, validator
        self.risk_gate, self.motor, self.evaluator = risk_gate, motor, evaluator
        self.clock, self.identifiers = clock, identifiers

    async def execute(self, cycle: CognitiveCycle, state: BrainState,
                      bindings: Mapping[tuple[str, str, str], SkillBinding], *,
                      dna_identity: object | None = None,
                      responsibility: str | None = None) -> GovernedExecutionResult:
        now = self.clock.now().astimezone(UTC)
        planned = await self.planner.plan(cycle, brain_mode=state.brain_mode.value,
                                          phase=state.phase.value)
        dna_document: Mapping[str, object] | None = None
        if dna_identity is not None:
            from active_agent_platform.dna_execution import (  # avoid module import cycle
                DnaExecutionIdentity,
                verify_execution_identity,
            )
            if not isinstance(dna_identity, DnaExecutionIdentity) or not responsibility:
                raise ValueError("complete DNA identity and responsibility are required")
            document = dict(planned.plan.document)
            identity_document = dna_identity.to_document()
            context_seed = identity_document | {
                "responsibility": responsibility, "plan_id": planned.plan.plan_id,
                "correlation_id": planned.plan.correlation_id,
            }
            context_digest = "sha256:" + hashlib.sha256(json.dumps(
                context_seed, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            dna_document = identity_document | {
                "context_digest": context_digest, "responsibility": responsibility,
            }
            document["dna_context"] = dna_document
            planned = replace(planned, plan=type(planned.plan).create(document))
        validated = self.validator.validate(planned.plan.document, now=now)
        approval = self.risk_gate.evaluate(validated, state=state, now=now)
        if len(validated.tasks) != 1:
            raise ValueError("GovernedCognitiveApp requires exactly one task")
        task = validated.tasks[0]
        if dna_identity is not None:
            await verify_execution_identity(
                self.database, dna_identity, workflow_id=task.workflow.workflow_id,
                workflow_version=task.workflow.version, workflow_digest=task.workflow.digest,
            )
        decision_id, grant_id = str(self.identifiers.new()), str(self.identifiers.new())
        correlation_id = planned.plan.correlation_id
        decision = PlanDecision.create({
            "schema_version": "1.0", "decision_id": decision_id,
            "plan_id": planned.plan.plan_id, "decision": "APPROVED",
            "decided_at": _time(now), "validator_version": "1.0",
            "policy_version": approval.policy_version,
            "world_snapshot_id": str(cycle.world_snapshot.version),
            "reasons": ["validated and admitted by RiskGate"],
            "correlation_id": correlation_id,
        })
        if dna_document is not None and isinstance(dna_identity, DnaExecutionIdentity):
            decision = PlanDecision.create(dict(decision.document) | {"dna_context": dna_document})
        selected = tuple(binding for key, binding in bindings.items()
                         if key[:2] == (task.workflow.workflow_id, task.workflow.version))
        grant = {
            "schema_version": "1.0", "grant_id": grant_id, "decision_id": decision_id,
            "plan_id": planned.plan.plan_id, "task_id": task.task_id,
            "workflow": {"workflow_id": task.workflow.workflow_id,
                         "version": task.workflow.version, "digest": task.workflow.digest},
            "bindings": [_binding(item) for item in selected],
            "policy_version": approval.policy_version,
            "world_snapshot_id": str(cycle.world_snapshot.version),
            "memory_snapshot_id": str(cycle.memory_snapshot.version),
            "allowed_permissions": sorted(approval.allowed_permissions),
            "budget": {"max_duration_seconds": approval.budget.max_duration_seconds,
                       "max_tokens": approval.budget.max_tokens,
                       "max_cost_minor": approval.budget.max_cost_minor, "currency": "CNY"},
            "issued_at": _time(now), "expires_at": _time(task.deadline),
            "consumption": "SINGLE_TASK_MULTI_ATTEMPT", "correlation_id": correlation_id,
        }
        if dna_document is not None:
            grant["dna_context"] = dna_document
        async with self.database.transaction() as transaction:
            repository = PlanningRepository(transaction)
            await repository.add_plan(planned.plan)
            await repository.add_decision(decision)
            await GrantIssuer(transaction).issue(grant)
        execution = await self.motor.execute(MotorExecutionRequest(
            grant_id, task.task_id, task.workflow, task.parameters, bindings,
            task.deadline, approval.allowed_permissions, priority=task.priority,
        ))
        evaluation = await self.evaluator.evaluate_and_record(OutcomeRequest(
            task.task_id, correlation_id, TaskStatus(execution.status.value),
            str(planned.plan.document["goal"]["goal_id"]),  # type: ignore[index]
            execution.status.value == "SUCCEEDED", {"execution": 1.0}, None, 0.0, (), (), 1,
        ))
        if dna_document is not None and isinstance(dna_identity, DnaExecutionIdentity):
            async with self.database.transaction() as transaction:
                await transaction.execute(
                    "INSERT INTO dna_execution_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(dna_document["context_digest"]), correlation_id,
                     planned.plan.plan_id, decision_id,
                     grant_id, task.task_id, execution.run_id, evaluation.episode_id,
                     evaluation.evaluation_id, dna_identity.organization.dna_id,
                     dna_identity.organization.version, dna_identity.organization.content_digest,
                     dna_identity.organization_role, dna_identity.agent.dna_id,
                     dna_identity.agent.version, dna_identity.agent.content_digest,
                     dna_identity.workflow.dna_id, dna_identity.workflow.version,
                     dna_identity.workflow.content_digest,
                     json.dumps(dna_document, sort_keys=True, separators=(",", ":"))),
                )
        return GovernedExecutionResult(planned, decision_id, grant_id, execution, evaluation)


def _binding(value: SkillBinding) -> dict[str, object]:
    return {"schema_version": "1.0", "node_id": value.node_id,
            "capability": value.capability, "capability_version": value.capability_version,
            "skill_id": value.skill_id, "skill_version": value.skill_version,
            "skill_digest": value.skill_digest,
            "binding_policy_version": value.binding_policy_version,
            "resolved_at": _time(value.resolved_at)}


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
