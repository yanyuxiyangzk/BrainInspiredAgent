"""Small injectable execution boundary shared by quant applications."""
from __future__ import annotations

from active_agent_platform.motor import MotorExec, MotorExecutionRequest
from active_agent_platform.workflow_runtime import WorkflowExecutionResult
from active_agent_platform.outcomes import OutcomeEvaluation, OutcomeEvaluator, OutcomeRequest


class QuantExecutionFacade:
    def __init__(self, motor: MotorExec, evaluator: OutcomeEvaluator | None = None) -> None:
        self.motor, self.evaluator = motor, evaluator

    async def execute(self, request: MotorExecutionRequest) -> WorkflowExecutionResult:
        return await self.motor.execute(request)

    async def evaluate(self, request: OutcomeRequest) -> OutcomeEvaluation:
        if self.evaluator is None:
            raise RuntimeError("outcome evaluator is not configured")
        return await self.evaluator.evaluate_and_record(request)
