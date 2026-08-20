"""Small injectable execution boundary shared by quant applications."""
from __future__ import annotations

from active_agent_platform.motor import MotorExec, MotorExecutionRequest
from active_agent_platform.outcomes import OutcomeEvaluation, OutcomeEvaluator, OutcomeRequest
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow_runtime import WorkflowExecutionResult
from domain_sdk.organization_execution import OrganizationGovernedApp


class QuantExecutionFacade:
    def __init__(self, motor: MotorExec, evaluator: OutcomeEvaluator | None = None,
                 governed: OrganizationGovernedApp | None = None,
                 database: SQLiteDatabase | None = None) -> None:
        self.motor, self.evaluator, self.governed, self.database = motor, evaluator, governed, database

    async def execute(self, request: MotorExecutionRequest) -> WorkflowExecutionResult:
        return await self.motor.execute(request)

    async def evaluate(self, request: OutcomeRequest) -> OutcomeEvaluation:
        if self.evaluator is None:
            raise RuntimeError("outcome evaluator is not configured")
        return await self.evaluator.evaluate_and_record(request)

    async def record_dna_context(self, context: dict[str, object]) -> None:
        if self.database is None:
            return
        required = ("context_digest", "correlation_id", "plan_id", "decision_id", "grant_id",
                    "task_id", "run_id", "episode_id", "evaluation_id", "organization_dna_id",
                    "organization_version", "organization_content_digest", "organization_role",
                    "agent_dna_id", "agent_version", "agent_content_digest", "workflow_dna_id",
                    "workflow_version", "workflow_content_digest")
        if not all(key in context for key in required):
            return
        async with self.database.transaction() as transaction:
            values = tuple(context[key] for key in required)
            await transaction.execute(
                "INSERT OR IGNORE INTO dna_execution_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(str(value) for value in values) + ("{}",),
            )
