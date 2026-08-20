"""Governed application service for the market-summary cognitive cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from active_agent_platform.coordinator import CognitiveCycle
from active_agent_platform.motor import MotorExec, MotorExecutionRequest, TaskStatus
from active_agent_platform.outcomes import OutcomeEvaluation, OutcomeEvaluator, OutcomeRequest
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.planning import (
    CandidatePlan,
    GrantIssuer,
    PlanDecision,
    PlanningRepository,
)
from active_agent_platform.prefrontal import PlannerResult, RulePlanner
from active_agent_platform.risk import RiskGate
from active_agent_platform.skills import SkillBinding
from active_agent_platform.state import BrainState
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow_runtime import WorkflowExecutionResult
from apps.quant_agent.execution_facade import QuantExecutionFacade
from brain_kernel.ports import Clock, UuidGenerator


@dataclass(frozen=True, slots=True)
class MarketSummaryResult:
    planner: PlannerResult
    decision_id: str
    grant_id: str
    execution: WorkflowExecutionResult
    outcome: OutcomeEvaluation


class MarketSummaryApp:
    """Join cognition, governance, execution and outcome without domain leakage."""

    def __init__(
        self,
        database: SQLiteDatabase,
        planner: RulePlanner,
        validator: PlanValidator,
        risk_gate: RiskGate,
        motor: MotorExec,
        evaluator: OutcomeEvaluator,
        clock: Clock,
        identifiers: UuidGenerator,
        execution_facade: QuantExecutionFacade | None = None,
    ) -> None:
        self._database = database
        self._planner = planner
        self._validator = validator
        self._risk_gate = risk_gate
        self._motor = motor
        self._evaluator = evaluator
        self._clock = clock
        self._identifiers = identifiers
        self._facade = execution_facade or QuantExecutionFacade(motor, evaluator)

    async def execute(
        self,
        cycle: CognitiveCycle,
        state: BrainState,
        bindings: Mapping[tuple[str, str, str], SkillBinding],
        *,
        planner: RulePlanner | None = None,
        dna_context: Mapping[str, object] | None = None,
    ) -> MarketSummaryResult:
        now = self._clock.now().astimezone(UTC)
        planned = await (planner or self._planner).plan(
            cycle, brain_mode=state.brain_mode.value, phase=state.phase.value
        )
        if dna_context is not None:
            planned = replace(planned, plan=CandidatePlan.create(
                dict(planned.plan.document) | {"dna_context": dict(dna_context)}))
        validated = self._validator.validate(planned.plan.document, now=now)
        approval = self._risk_gate.evaluate(validated, state=state, now=now)
        task = validated.tasks[0]
        decision_id, grant_id = str(self._identifiers.new()), str(self._identifiers.new())
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
        selected = tuple(
            binding for key, binding in bindings.items()
            if key[0] == task.workflow.workflow_id and key[1] == task.workflow.version
        )
        grant = {
            "schema_version": "1.0", "grant_id": grant_id,
            "decision_id": decision_id, "plan_id": planned.plan.plan_id,
            "task_id": task.task_id,
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
        async with self._database.transaction() as transaction:
            repository = PlanningRepository(transaction)
            await repository.add_plan(planned.plan)
            await repository.add_decision(decision)
            await GrantIssuer(transaction).issue(grant)
        execution = await self._facade.execute(MotorExecutionRequest(
            grant_id, task.task_id, task.workflow, task.parameters, bindings,
            task.deadline, approval.allowed_permissions, priority=task.priority,
        ))
        evaluation = await self._facade.evaluate(OutcomeRequest(
            task.task_id, correlation_id, TaskStatus(execution.status.value),
            str(planned.plan.document["goal"]["goal_id"]),  # type: ignore[index]
            execution.status.value == "SUCCEEDED", {"delivery": 1.0}, None, 0.0, (),
            (str(execution.output.get("notification_id", "")),), 1,
        ))
        return MarketSummaryResult(planned, decision_id, grant_id, execution, evaluation)


def _binding(value: SkillBinding) -> dict[str, object]:
    return {
        "schema_version": "1.0", "node_id": value.node_id,
        "capability": value.capability, "capability_version": value.capability_version,
        "skill_id": value.skill_id, "skill_version": value.skill_version,
        "skill_digest": value.skill_digest,
        "binding_policy_version": value.binding_policy_version,
        "resolved_at": _time(value.resolved_at),
    }


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
