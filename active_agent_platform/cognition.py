"""Deterministic WorldModel projection and explainable Attention rules."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

from active_agent_platform.events import EventEnvelope
from brain_kernel.ports import Clock, UuidGenerator


class FactOutcome(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    CONFLICT = "CONFLICT"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class FactRecord:
    key: str
    value: Mapping[str, object]
    source_msg_id: str
    observed_at: datetime
    data_quality: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", MappingProxyType(dict(self.value)))


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    version: int
    created_at: datetime
    facts: Mapping[str, FactRecord]

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))

    def get(self, key: str) -> FactRecord | None:
        return self.facts.get(key)


@dataclass(frozen=True, slots=True)
class WorldUpdate:
    outcome: FactOutcome
    snapshot: WorldSnapshot
    key: str | None = None
    reason: str | None = None


class WorldModel:
    def __init__(self, clock: Clock, *, freshness_seconds: float = 60.0) -> None:
        if freshness_seconds < 0:
            raise ValueError("freshness_seconds must be non-negative")
        self._clock = clock
        self._freshness = freshness_seconds
        now = clock.now().astimezone(UTC)
        self._snapshot = WorldSnapshot(0, now, {})
        self._seen: set[str] = set()
        self._last_by_key: dict[str, FactRecord] = {}

    @property
    def snapshot(self) -> WorldSnapshot:
        return self._snapshot

    def apply(self, event: EventEnvelope) -> WorldUpdate:
        if event.msg_type != "perception.snapshot" or event.payload.get("event_type") != event.msg_type:
            return WorldUpdate(FactOutcome.INVALID, self._snapshot, reason="unsupported event type")
        data = event.payload.get("data")
        if not isinstance(data, Mapping):
            return WorldUpdate(FactOutcome.INVALID, self._snapshot, reason="payload data is not an object")
        if event.msg_id in self._seen:
            return WorldUpdate(FactOutcome.DUPLICATE, self._snapshot, reason="message already projected")
        key = _fact_key(event)
        observed_at = event.occurred_at.astimezone(UTC)
        previous = self._last_by_key.get(key)
        if previous is not None and observed_at < previous.observed_at:
            return WorldUpdate(FactOutcome.OUT_OF_ORDER, self._snapshot, key, "event time is older than current fact")
        if previous is not None and observed_at == previous.observed_at:
            if dict(previous.value) == dict(data):
                self._seen.add(event.msg_id)
                return WorldUpdate(FactOutcome.DUPLICATE, self._snapshot, key, "same fact timestamp and value")
            return WorldUpdate(FactOutcome.CONFLICT, self._snapshot, key, "same timestamp has different values")
        now = self._clock.now().astimezone(UTC)
        expires_at = now + timedelta(seconds=self._freshness) if self._freshness else now
        record = FactRecord(key, dict(data), event.msg_id, observed_at, str(event.payload.get("data_quality", "VALID")), expires_at)
        facts = dict(self._snapshot.facts)
        facts[key] = record
        self._last_by_key[key] = record
        self._seen.add(event.msg_id)
        self._snapshot = WorldSnapshot(self._snapshot.version + 1, now, facts)
        return WorldUpdate(FactOutcome.APPLIED, self._snapshot, key)

    def fresh_snapshot(self) -> WorldSnapshot:
        now = self._clock.now().astimezone(UTC)
        facts = {
            key: record
            for key, record in self._snapshot.facts.items()
            if record.expires_at is None or record.expires_at > now
        }
        if len(facts) == len(self._snapshot.facts):
            return self._snapshot
        self._snapshot = WorldSnapshot(self._snapshot.version + 1, now, facts)
        return self._snapshot


class AttentionOutcome(StrEnum):
    SALIENT = "SALIENT"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    DUPLICATE = "DUPLICATE"
    COOLDOWN = "COOLDOWN"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    rule_id: str
    value_field: str
    threshold: float
    relative: bool = True
    cooldown_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.rule_id or not self.value_field:
            raise ValueError("rule_id and value_field must not be empty")
        if self.threshold < 0 or not isfinite(self.threshold):
            raise ValueError("threshold must be finite and non-negative")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class AttentionResult:
    outcome: AttentionOutcome
    rule_id: str
    score: float = 0.0
    baseline: float | None = None
    current: float | None = None
    evidence_msg_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AttentionMetrics:
    evaluated: int
    salient: int
    below_threshold: int
    duplicate: int
    cooldown: int
    invalid: int


EventSink = Callable[[EventEnvelope], Awaitable[object]]


class Attention:
    def __init__(
        self,
        clock: Clock,
        uuid: UuidGenerator,
        rules: tuple[ThresholdRule, ...],
        *,
        sink: EventSink | None = None,
        source: str = "attention",
    ) -> None:
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("attention rule IDs must be unique")
        if not source:
            raise ValueError("source must not be empty")
        self._clock = clock
        self._uuid = uuid
        self._rules = rules
        self._sink = sink
        self._source = source
        self._seen: set[tuple[str, str]] = set()
        self._last_salient: dict[tuple[str, str], datetime] = {}
        self._counts = {outcome: 0 for outcome in AttentionOutcome}

    def metrics(self) -> AttentionMetrics:
        return AttentionMetrics(
            sum(self._counts.values()),
            self._counts[AttentionOutcome.SALIENT],
            self._counts[AttentionOutcome.BELOW_THRESHOLD],
            self._counts[AttentionOutcome.DUPLICATE],
            self._counts[AttentionOutcome.COOLDOWN],
            self._counts[AttentionOutcome.INVALID],
        )

    async def process(self, event: EventEnvelope, snapshot: WorldSnapshot) -> tuple[AttentionResult, ...]:
        data = event.payload.get("data")
        if not isinstance(data, Mapping):
            invalid_results = tuple(AttentionResult(AttentionOutcome.INVALID, rule.rule_id, reason="data is not an object") for rule in self._rules)
            self._record(invalid_results)
            return invalid_results
        entity = _fact_key(event)
        results: list[AttentionResult] = []
        for rule in self._rules:
            current = _number(data.get(rule.value_field))
            previous = snapshot.get(entity)
            baseline = _number(previous.value.get(rule.value_field)) if previous else None
            if current is None or baseline is None:
                results.append(AttentionResult(AttentionOutcome.INVALID, rule.rule_id, reason="numeric baseline/current required"))
                continue
            score = abs(current - baseline) / abs(baseline) if rule.relative and baseline != 0 else abs(current - baseline)
            identity = (rule.rule_id, event.dedup_key)
            if identity in self._seen:
                results.append(AttentionResult(AttentionOutcome.DUPLICATE, rule.rule_id, score, baseline, current, (event.msg_id,), "dedup key already evaluated"))
                continue
            self._seen.add(identity)
            now = self._clock.now().astimezone(UTC)
            last = self._last_salient.get((rule.rule_id, entity))
            if last is not None and (now - last).total_seconds() < rule.cooldown_seconds:
                results.append(AttentionResult(AttentionOutcome.COOLDOWN, rule.rule_id, score, baseline, current, (event.msg_id,), "rule cooldown active"))
                continue
            if score < rule.threshold:
                results.append(AttentionResult(AttentionOutcome.BELOW_THRESHOLD, rule.rule_id, score, baseline, current, (event.msg_id,), "score below threshold"))
                continue
            self._last_salient[(rule.rule_id, entity)] = now
            result = AttentionResult(AttentionOutcome.SALIENT, rule.rule_id, score, baseline, current, (event.msg_id,), "threshold exceeded")
            results.append(result)
            if self._sink is not None:
                msg_id = str(self._uuid.new())
                await self._sink(
                    EventEnvelope(
                        msg_id=msg_id,
                        msg_type="attention.salient_event",
                        source=self._source,
                        occurred_at=event.occurred_at,
                        published_at=now,
                        priority=event.priority,
                        correlation_id=event.correlation_id,
                        causation_id=event.msg_id,
                        dedup_key=f"{rule.rule_id}:{event.dedup_key}",
                        payload={
                            "event_type": "attention.salient_event",
                            "stimulus_id": f"{rule.rule_id}:{event.dedup_key}",
                            "data": {
                                "rule_id": rule.rule_id,
                                "score": round(score, 8),
                                "baseline": baseline,
                                "current": current,
                                "evidence_msg_ids": [event.msg_id],
                                "reason": result.reason,
                            },
                            "data_quality": "VALID",
                        },
                    )
                )
        completed = tuple(results)
        self._record(completed)
        return completed

    def _record(self, results: tuple[AttentionResult, ...]) -> None:
        for result in results:
            self._counts[result.outcome] += 1


def _fact_key(event: EventEnvelope) -> str:
    data = event.payload.get("data")
    if isinstance(data, Mapping):
        for field in ("entity_id", "instrument", "id"):
            value = data.get(field)
            if isinstance(value, str) and value:
                return value
    return str(event.payload.get("stimulus_id", event.dedup_key))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(float(value)):
        return None
    return float(value)
