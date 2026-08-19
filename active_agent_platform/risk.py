"""F05 deterministic capability, mode, freshness and budget RiskGate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from active_agent_platform.plan_validation import ValidatedPlan
from active_agent_platform.state import BrainMode, BrainState


@dataclass(frozen=True, slots=True)
class RiskBudget:
    max_tokens: int
    max_cost_minor: int
    max_duration_seconds: int


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: str
    allowed_capabilities: frozenset[str]
    allowed_permissions: frozenset[str]
    plan_budget: RiskBudget
    daily_remaining: RiskBudget
    safe_capabilities: frozenset[str] = frozenset()
    max_node_duration_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RiskApproval:
    policy_version: str
    allowed_permissions: frozenset[str]
    budget: RiskBudget


class RiskRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RiskGate:
    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, plan: ValidatedPlan, *, state: BrainState, now: datetime) -> RiskApproval:
        context = plan.plan.document["policy_context"]
        if not isinstance(context, Mapping):
            raise RiskRejected("PLAN_SCHEMA_INVALID", "policy_context must be an object")
        fresh_until = datetime.fromisoformat(str(context["data_fresh_until"]))
        if now > fresh_until:
            raise RiskRejected("DATA_STALE", "plan data is stale")
        capabilities: set[str] = set()
        for task in plan.tasks:
            policy = task.workflow.definition["policy"]
            assert isinstance(policy, Mapping)
            required = policy["required_capabilities"]
            assert isinstance(required, Sequence) and not isinstance(required, str | bytes)
            capabilities.update(str(item) for item in required)
            nodes = task.workflow.definition["nodes"]
            assert isinstance(nodes, tuple | list)
            if any(
                isinstance(node, Mapping)
                and int(node.get("timeout_seconds", self._policy.max_node_duration_seconds))
                > self._policy.max_node_duration_seconds
                for node in nodes
            ):
                raise RiskRejected("BUDGET_EXCEEDED", "node timeout exceeds policy budget")
        if not capabilities <= self._policy.allowed_capabilities:
            raise RiskRejected("CAPABILITY_DENIED", "plan requests a denied capability")
        if state.brain_mode is BrainMode.SAFE and not capabilities <= self._policy.safe_capabilities:
            raise RiskRejected("BRAIN_MODE_DENIED", "SAFE mode denies this plan")
        requested = plan.plan.document["requested_budget"]
        if not isinstance(requested, Mapping):
            raise RiskRejected("PLAN_SCHEMA_INVALID", "requested_budget must be an object")
        budget = RiskBudget(
            int(requested["max_tokens"]),
            int(requested["max_cost_minor"]),
            int(requested["max_duration_seconds"]),
        )
        for limit in (self._policy.plan_budget, self._policy.daily_remaining):
            if (
                budget.max_tokens > limit.max_tokens
                or budget.max_cost_minor > limit.max_cost_minor
                or budget.max_duration_seconds > limit.max_duration_seconds
            ):
                raise RiskRejected("BUDGET_EXCEEDED", "plan exceeds an approved budget")
        return RiskApproval(self._policy.version, self._policy.allowed_permissions, budget)
