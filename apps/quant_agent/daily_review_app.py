"""Governed application service for one restart-idempotent daily review."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from active_agent_platform.motor import MotorExec, MotorExecutionRequest
from active_agent_platform.plan_validation import PlanValidator
from active_agent_platform.planning import (
    CandidatePlan,
    GrantIssuer,
    PlanDecision,
    PlanningRepository,
)
from active_agent_platform.rest_repair import RepairDecision, RepairOutcome, RestRepair
from active_agent_platform.risk import RiskGate
from active_agent_platform.skills import SkillBinding
from active_agent_platform.state import BrainState
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow_runtime import WorkflowExecutionResult
from brain_kernel.ports import Clock, UuidGenerator


@dataclass(frozen=True, slots=True)
class DailyReviewResult:
    decision: RepairDecision
    execution: WorkflowExecutionResult | None
    candidate_experiences: tuple[Mapping[str, object], ...]


class DailyReviewApp:
    def __init__(
        self,
        database: SQLiteDatabase,
        repair: RestRepair,
        validator: PlanValidator,
        risk_gate: RiskGate,
        motor: MotorExec,
        clock: Clock,
        identifiers: UuidGenerator,
    ) -> None:
        self._database = database
        self._repair = repair
        self._validator = validator
        self._risk_gate = risk_gate
        self._motor = motor
        self._clock = clock
        self._identifiers = identifiers

    async def execute(
        self,
        business_date: date,
        state: BrainState,
        bindings: Mapping[tuple[str, str, str], SkillBinding],
    ) -> DailyReviewResult:
        decision = await self._repair.prepare(
            business_date, mode=state.brain_mode.value, phase=state.phase.value
        )
        if decision.outcome is not RepairOutcome.REQUESTED or decision.request is None:
            return DailyReviewResult(decision, None, ())
        request = decision.request
        now = self._clock.now().astimezone(UTC)
        plan_id, task_id = str(self._identifiers.new()), str(self._identifiers.new())
        plan = CandidatePlan.create({
            "schema_version": "1.0", "plan_id": plan_id, "status": "CANDIDATE",
            "created_at": _time(now), "expires_at": _time(request.deadline),
            "correlation_id": request.correlation_id,
            "trigger": {"type": "SCHEDULE", "source_id": request.run_id,
                        "occurred_at": _time(now)},
            "goal": {"goal_id": "daily.review", "priority": 60},
            "reason": "review daily outcome episodes", "evidence": [],
            "tasks": [{"task_id": task_id, "workflow_id": request.workflow_id,
                       "workflow_version": request.workflow_version,
                       "params": dict(request.parameters), "priority": 60,
                       "deadline": _time(request.deadline),
                       "idempotency_key": request.review_key, "depends_on": []}],
            "requested_budget": {"max_tokens": 100, "max_cost_minor": 10,
                                 "currency": "CNY", "max_duration_seconds": 60},
            "policy_context": {"brain_mode": state.brain_mode.value,
                               "market_phase": state.phase.value,
                               "data_fresh_until": _time(request.deadline)},
        })
        validated = self._validator.validate(plan.document, now=now)
        approval = self._risk_gate.evaluate(validated, state=state, now=now)
        task = validated.tasks[0]
        decision_id, grant_id = str(self._identifiers.new()), str(self._identifiers.new())
        plan_decision = PlanDecision.create({
            "schema_version": "1.0", "decision_id": decision_id, "plan_id": plan_id,
            "decision": "APPROVED", "decided_at": _time(now), "validator_version": "1.0",
            "policy_version": approval.policy_version, "world_snapshot_id": "daily-review",
            "reasons": ["daily review admitted"], "correlation_id": request.correlation_id,
        })
        selected = tuple(value for key, value in bindings.items()
                         if key[:2] == (task.workflow.workflow_id, task.workflow.version))
        grant = {
            "schema_version": "1.0", "grant_id": grant_id, "decision_id": decision_id,
            "plan_id": plan_id, "task_id": task_id,
            "workflow": {"workflow_id": task.workflow.workflow_id,
                         "version": task.workflow.version, "digest": task.workflow.digest},
            "bindings": [_binding(item) for item in selected],
            "policy_version": approval.policy_version, "world_snapshot_id": "daily-review",
            "memory_snapshot_id": "episodes", "allowed_permissions": sorted(approval.allowed_permissions),
            "budget": {"max_duration_seconds": approval.budget.max_duration_seconds,
                       "max_tokens": approval.budget.max_tokens,
                       "max_cost_minor": approval.budget.max_cost_minor, "currency": "CNY"},
            "issued_at": _time(now), "expires_at": _time(request.deadline),
            "consumption": "SINGLE_TASK_MULTI_ATTEMPT", "correlation_id": request.correlation_id,
        }
        async with self._database.transaction() as transaction:
            repository = PlanningRepository(transaction)
            await repository.add_plan(plan)
            await repository.add_decision(plan_decision)
            await GrantIssuer(transaction).issue(grant)
        execution = await self._motor.execute(MotorExecutionRequest(
            grant_id, task_id, task.workflow, task.parameters, bindings, task.deadline,
            approval.allowed_permissions, priority=task.priority,
        ))
        candidates = _candidates(decision)
        if execution.status.value == "SUCCEEDED":
            await self._repair.complete(
                request.run_id, result=execution.output, candidate_experiences=candidates
            )
        else:
            await self._repair.fail(request.run_id, error_code="WORKFLOW_FAILED")
        return DailyReviewResult(decision, execution, candidates)


def _candidates(decision: RepairDecision) -> tuple[Mapping[str, object], ...]:
    summary = decision.summary
    if summary is None or not summary.episode_ids:
        return ()
    return ({"status": "CANDIDATE", "evidence_episode_ids": list(summary.episode_ids),
             "confidence": 0.5, "scope": {"business_date": summary.business_date.isoformat()},
             "valid_until": f"{summary.business_date.isoformat()}T23:59:59Z"},)


def _binding(value: SkillBinding) -> dict[str, object]:
    return {"schema_version": "1.0", "node_id": value.node_id,
            "capability": value.capability, "capability_version": value.capability_version,
            "skill_id": value.skill_id, "skill_version": value.skill_version,
            "skill_digest": value.skill_digest,
            "binding_policy_version": value.binding_policy_version,
            "resolved_at": _time(value.resolved_at)}


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
