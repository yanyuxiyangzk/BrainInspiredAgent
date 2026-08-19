"""F03 deterministic prefrontal planning with an optional structured model."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast

from active_agent_platform.coordinator import CognitiveCycle
from active_agent_platform.planning import CandidatePlan, PlanningError
from brain_kernel.ports import Clock, UuidGenerator


class PlannerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanSource(StrEnum):
    RULE = "RULE"
    MODEL = "MODEL"
    RULE_FALLBACK = "RULE_FALLBACK"


class StructuredModel(Protocol):
    async def generate(self, request: Mapping[str, object]) -> str | Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class PlanningRule:
    rule_id: str
    goal_id: str
    workflow_id: str
    workflow_version: str
    parameters: Mapping[str, object]
    reason: str
    idempotency_prefix: str
    trigger_types: tuple[str, ...] = ()
    plan_ttl_seconds: int = 600
    task_ttl_seconds: int = 300
    data_freshness_seconds: int = 60
    use_model: bool = False

    def __post_init__(self) -> None:
        if not all((self.rule_id, self.goal_id, self.workflow_id, self.workflow_version)):
            raise ValueError("planning rule identifiers must not be empty")
        if min(self.plan_ttl_seconds, self.task_ttl_seconds, self.data_freshness_seconds) < 1:
            raise ValueError("planning rule durations must be positive")
        if self.task_ttl_seconds > self.plan_ttl_seconds:
            raise ValueError("task TTL cannot exceed plan TTL")
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters))


@dataclass(frozen=True, slots=True)
class PlannerResult:
    plan: CandidatePlan
    source: PlanSource
    rule_id: str
    model_error: str | None = None


@dataclass(frozen=True, slots=True)
class _Proposal:
    workflow_id: str
    workflow_version: str
    parameters: Mapping[str, object]
    reason: str


class FakeStructuredModel:
    """Deterministic, network-free structured model for tests and local E2E."""

    def __init__(self, responses: Sequence[str | Mapping[str, object] | Exception]) -> None:
        self._responses = iter(responses)
        self.requests: list[Mapping[str, object]] = []

    async def generate(self, request: Mapping[str, object]) -> str | Mapping[str, object]:
        self.requests.append(_freeze_mapping(request))
        try:
            response = next(self._responses)
        except StopIteration as error:
            raise PlannerError("MODEL_UNAVAILABLE", "fake model response sequence exhausted") from error
        if isinstance(response, Exception):
            raise response
        return response


class RulePlanner:
    """Create one immutable CandidatePlan from a frozen cognitive cycle.

    The planner never resolves or invokes a Skill. Registry, parameter and risk
    enforcement remain the responsibility of F04/F05.
    """

    def __init__(
        self,
        clock: Clock,
        uuid: UuidGenerator,
        rules: Sequence[PlanningRule],
        *,
        model: StructuredModel | None = None,
    ) -> None:
        if not rules:
            raise ValueError("at least one planning rule is required")
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("planning rule IDs must be unique")
        self._clock = clock
        self._uuid = uuid
        self._rules = tuple(rules)
        self._model = model

    async def plan(
        self,
        cycle: CognitiveCycle,
        *,
        brain_mode: str,
        phase: str,
    ) -> PlannerResult:
        rule = self._select_rule(cycle)
        proposal = _Proposal(
            rule.workflow_id, rule.workflow_version, rule.parameters, rule.reason
        )
        source = PlanSource.RULE
        model_error: str | None = None
        if rule.use_model and self._model is not None:
            try:
                proposal = _parse_proposal(await self._model.generate(_model_request(cycle, rule)))
                source = PlanSource.MODEL
            except Exception as error:  # noqa: BLE001 - adapter is a fault boundary
                source = PlanSource.RULE_FALLBACK
                model_error = _safe_model_error(error)
        elif rule.use_model:
            source = PlanSource.RULE_FALLBACK
            model_error = "MODEL_UNAVAILABLE"

        now = _utc(self._clock.now())
        focus = next(item for item in cycle.stimuli if item.msg_id == cycle.focus_msg_id)
        goal = cycle.goal_snapshot.get(rule.goal_id)
        if goal is None or rule.goal_id not in cycle.selected_goal_ids:
            raise PlannerError("GOAL_NOT_AVAILABLE", "planning goal is not selected in this cycle")
        plan_expiry = _bounded_expiry(now, rule.plan_ttl_seconds, goal.goal.deadline)
        task_expiry = min(now + timedelta(seconds=rule.task_ttl_seconds), plan_expiry)
        if task_expiry <= now:
            raise PlannerError("GOAL_EXPIRED", "goal deadline leaves no execution window")
        correlation_id = focus.correlation_id
        plan_id, task_id = str(self._uuid.new()), str(self._uuid.new())
        document: dict[str, object] = {
            "schema_version": "1.0",
            "plan_id": plan_id,
            "status": "CANDIDATE",
            "created_at": _iso(now),
            "expires_at": _iso(plan_expiry),
            "correlation_id": correlation_id,
            "trigger": {
                "type": _trigger_type(focus.msg_type),
                "source_id": focus.msg_id,
                "occurred_at": _iso(focus.occurred_at),
            },
            "goal": {"goal_id": rule.goal_id, "priority": goal.goal.priority},
            "reason": proposal.reason,
            "evidence": _evidence(cycle, rule),
            "tasks": [{
                "task_id": task_id,
                "workflow_id": proposal.workflow_id,
                "workflow_version": proposal.workflow_version,
                "params": _plain(proposal.parameters),
                "priority": goal.goal.priority,
                "deadline": _iso(task_expiry),
                "idempotency_key": f"{rule.idempotency_prefix}:{focus.dedup_key}",
                "depends_on": [],
            }],
            "requested_budget": goal.goal.budget.to_dict(),
            "policy_context": {
                "brain_mode": brain_mode,
                "m" + "arket_phase": phase,
                "data_fresh_until": _iso(_fresh_until(cycle, now, rule.data_freshness_seconds)),
            },
        }
        try:
            plan = CandidatePlan.create(document)
        except PlanningError as error:
            raise PlannerError(error.code, str(error)) from error
        return PlannerResult(plan, source, rule.rule_id, model_error)

    def _select_rule(self, cycle: CognitiveCycle) -> PlanningRule:
        focus = next((item for item in cycle.stimuli if item.msg_id == cycle.focus_msg_id), None)
        if focus is None:
            raise PlannerError("CYCLE_INVALID", "cycle focus message is absent")
        candidates = [
            rule for rule in self._rules
            if rule.goal_id in cycle.selected_goal_ids
            and (not rule.trigger_types or focus.msg_type in rule.trigger_types)
        ]
        if not candidates:
            raise PlannerError("NO_PLANNING_RULE", "no deterministic rule matches this cycle")
        return min(candidates, key=lambda item: item.rule_id)


def _parse_proposal(raw: str | Mapping[str, object]) -> _Proposal:
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise PlannerError("MODEL_OUTPUT_INVALID", "model output is not valid JSON") from error
    if not isinstance(value, dict):
        raise PlannerError("MODEL_OUTPUT_INVALID", "model output must be an object")
    required = {"workflow_id", "workflow_version", "params", "reason"}
    if set(value) != required:
        raise PlannerError("MODEL_OUTPUT_INVALID", "model output fields do not match contract")
    if not all(isinstance(value[key], str) and value[key] for key in ("workflow_id", "workflow_version", "reason")):
        raise PlannerError("MODEL_OUTPUT_INVALID", "model text fields must not be empty")
    params = value["params"]
    if not isinstance(params, Mapping):
        raise PlannerError("MODEL_OUTPUT_INVALID", "model params must be an object")
    return _Proposal(
        cast(str, value["workflow_id"]), cast(str, value["workflow_version"]),
        _freeze_mapping(params), cast(str, value["reason"]),
    )


def _model_request(cycle: CognitiveCycle, rule: PlanningRule) -> Mapping[str, object]:
    return _freeze_mapping({
        "contract": "workflow_proposal/1.0",
        "cognitive_cycle_id": cycle.cognitive_cycle_id,
        "focus_msg_id": cycle.focus_msg_id,
        "selected_goal_ids": list(cycle.selected_goal_ids),
        "world_snapshot_version": cycle.world_snapshot.version,
        "memory_snapshot_version": cycle.memory_snapshot.version,
        "rule": {
            "rule_id": rule.rule_id,
            "workflow_id": rule.workflow_id,
            "workflow_version": rule.workflow_version,
            "params": _plain(rule.parameters),
        },
    })


def _evidence(cycle: CognitiveCycle, rule: PlanningRule) -> list[dict[str, str]]:
    evidence = [{"type": "RULE", "id": rule.rule_id, "summary": "deterministic planning rule"}]
    evidence.extend(
        {"type": "MESSAGE", "id": event.msg_id, "summary": event.msg_type}
        for event in cycle.stimuli[:99]
    )
    return evidence[:100]


def _fresh_until(cycle: CognitiveCycle, now: datetime, seconds: int) -> datetime:
    expiries = [fact.expires_at for fact in cycle.world_snapshot.facts.values() if fact.expires_at]
    return min(expiries, default=now + timedelta(seconds=seconds))


def _bounded_expiry(now: datetime, seconds: int, deadline: datetime | None) -> datetime:
    expiry = now + timedelta(seconds=seconds)
    return min(expiry, _utc(deadline)) if deadline is not None else expiry


def _trigger_type(msg_type: str) -> str:
    return {
        "attention.salient_event": "SALIENT_EVENT",
        "command.received": "EXTERNAL_COMMAND",
        "schedule.triggered": "SCHEDULE",
        "goal.changed": "GOAL",
    }.get(msg_type, "GOAL")


def _safe_model_error(error: Exception) -> str:
    code = getattr(error, "code", None)
    return str(code) if code else type(error).__name__


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlannerError("TIME_INVALID", "planner timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value
