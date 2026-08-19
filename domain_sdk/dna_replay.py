"""Historical, deterministic sandbox comparison for parent and Candidate DNA."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock
from domain_sdk.dna import DnaDefinition, DnaStatus
from domain_sdk.dna_candidates import CandidateProposal
from domain_sdk.experience_dataset import DatasetSplit, ExperienceDataset, ExperienceSample


class DnaReplayError(ValueError):
    pass


class FaultScenario(StrEnum):
    NONE = "NONE"
    TIMEOUT = "TIMEOUT"
    SKILL_FAILURE = "SKILL_FAILURE"
    CORRUPT_OUTPUT = "CORRUPT_OUTPUT"
    CANCELLED = "CANCELLED"


class ReplayStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ReplayContext:
    replay_id: str
    sample_id: str
    virtual_time: datetime
    deterministic_seed: str
    fault: FaultScenario


@dataclass(frozen=True, slots=True)
class ReplayMeasurement:
    successful: bool
    evidence_score: float
    user_value_score: float
    cost_minor: int
    latency_ms: int
    stable: bool
    risk_violations: tuple[str, ...]
    output_digest: str

    def __post_init__(self) -> None:
        if not 0 <= self.evidence_score <= 1 or not 0 <= self.user_value_score <= 1:
            raise DnaReplayError("replay scores must be between zero and one")
        if self.cost_minor < 0 or self.latency_ms < 0:
            raise DnaReplayError("replay cost and latency must be non-negative")
        if len(set(self.risk_violations)) != len(self.risk_violations):
            raise DnaReplayError("replay risk violations must be unique")
        if not self.output_digest.startswith("sha256:"):
            raise DnaReplayError("replay output digest is invalid")

    def to_document(self) -> dict[str, object]:
        return {
            "successful": self.successful, "evidence_score": self.evidence_score,
            "user_value_score": self.user_value_score, "cost_minor": self.cost_minor,
            "latency_ms": self.latency_ms, "stable": self.stable,
            "risk_violations": list(self.risk_violations),
            "output_digest": self.output_digest,
        }


class SandboxExecutor(Protocol):
    async def execute(
        self, dna: DnaDefinition, sample: ExperienceSample, context: ReplayContext,
    ) -> ReplayMeasurement: ...


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    policy_version: str
    repetitions: int = 2
    minimum_cases: int = 1
    minimum_success_delta: float = 0.0
    minimum_evidence_delta: float = 0.0
    minimum_value_delta: float = 0.0
    minimum_stability_delta: float = 0.0
    maximum_cost_increase_ratio: float = 0.0
    maximum_latency_increase_ratio: float = 0.0
    maximum_candidate_risk_rate: float = 0.0
    included_splits: frozenset[DatasetSplit] = frozenset({DatasetSplit.VALIDATION,
                                                          DatasetSplit.TEST})
    allow_non_replayable_simulation: bool = False

    def __post_init__(self) -> None:
        if not self.policy_version or self.repetitions < 2 or self.minimum_cases < 1:
            raise DnaReplayError("replay policy identifiers and counts are invalid")
        if not self.included_splits or DatasetSplit.TRAIN in self.included_splits:
            raise DnaReplayError("sandbox replay cannot use the training split")
        ratios = (self.maximum_cost_increase_ratio, self.maximum_latency_increase_ratio,
                  self.maximum_candidate_risk_rate)
        if any(value < 0 for value in ratios) or self.maximum_candidate_risk_rate > 1:
            raise DnaReplayError("replay policy ratios are invalid")


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    replay_id: str
    proposal: CandidateProposal
    parent: DnaDefinition
    dataset: ExperienceDataset
    correlation_id: str
    faults: Mapping[str, FaultScenario] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_.:-]{3,128}", self.replay_id) is None \
                or not self.correlation_id:
            raise DnaReplayError("replay request metadata is invalid")
        object.__setattr__(self, "faults", MappingProxyType(dict(self.faults)))


@dataclass(frozen=True, slots=True)
class ReplayVector:
    success_rate: float
    evidence_score: float
    user_value_score: float
    average_cost_minor: float
    average_latency_ms: float
    p95_latency_ms: int
    stability_rate: float
    risk_rate: float

    def to_document(self) -> dict[str, object]:
        return {
            "success_rate": self.success_rate, "evidence_score": self.evidence_score,
            "user_value_score": self.user_value_score,
            "average_cost_minor": self.average_cost_minor,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "stability_rate": self.stability_rate, "risk_rate": self.risk_rate,
        }


@dataclass(frozen=True, slots=True)
class ReplayCase:
    sample_id: str
    ordinal: int
    split: DatasetSplit
    context: ReplayContext
    parent: ReplayMeasurement
    candidate: ReplayMeasurement
    parent_deterministic: bool
    candidate_deterministic: bool
    case_digest: str


@dataclass(frozen=True, slots=True)
class ReplayReport:
    replay_id: str
    proposal_id: str
    dataset_manifest_digest: str
    policy_version: str
    status: ReplayStatus
    reasons: tuple[str, ...]
    parent: ReplayVector
    candidate: ReplayVector
    deltas: Mapping[str, float]
    cases: tuple[ReplayCase, ...]
    started_at: datetime
    finished_at: datetime
    report_digest: str


class DnaSandboxReplay:
    def __init__(
        self, database: SQLiteDatabase, clock: Clock, executor: SandboxExecutor,
        policy: ReplayPolicy,
    ) -> None:
        self._database = database
        self._clock = clock
        self._executor = executor
        self._policy = policy

    async def run(self, request: ReplayRequest) -> ReplayReport:
        selected = tuple(sample for sample in request.dataset.samples
                         if sample.split in self._policy.included_splits)
        if len(selected) < self._policy.minimum_cases:
            raise DnaReplayError("sandbox replay has insufficient validation/test cases")
        if not set(request.faults) <= {sample.sample_id for sample in selected}:
            raise DnaReplayError("fault injection references an unselected sample")
        request_digest = _digest(_request_document(request, self._policy, selected))
        async with self._database.transaction() as transaction:
            await self._validate_sources(transaction, request, selected)
            existing = await transaction.fetch_one(
                "SELECT request_digest FROM dna_replay_run WHERE replay_id=?",
                (request.replay_id,),
            )
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise DnaReplayError("replay ID already exists with another request")
                return await self._load(transaction, request.replay_id)
        started = _utc(self._clock.now())
        base = _base_parent(request)
        cases: list[ReplayCase] = []
        for ordinal, sample in enumerate(selected):
            fault = request.faults.get(sample.sample_id, FaultScenario.NONE)
            seed = _digest({"replay_id": request.replay_id, "sample_id": sample.sample_id})
            context = ReplayContext(request.replay_id, sample.sample_id,
                                    sample.observed_at, seed, fault)
            parent_runs = [await self._executor.execute(base, sample, context)
                           for _ in range(self._policy.repetitions)]
            candidate_runs = [await self._executor.execute(
                request.proposal.candidate, sample, context
            ) for _ in range(self._policy.repetitions)]
            parent, candidate = parent_runs[0], candidate_runs[0]
            case_document = {
                "sample_id": sample.sample_id, "split": sample.split.value,
                "virtual_time": _time(context.virtual_time), "seed": seed,
                "fault": fault.value, "parent": parent.to_document(),
                "candidate": candidate.to_document(),
                "parent_deterministic": all(item == parent for item in parent_runs),
                "candidate_deterministic": all(item == candidate for item in candidate_runs),
            }
            cases.append(ReplayCase(
                sample.sample_id, ordinal, sample.split, context, parent, candidate,
                cast(bool, case_document["parent_deterministic"]),
                cast(bool, case_document["candidate_deterministic"]),
                _digest(case_document),
            ))
        parent_vector = _vector([item.parent for item in cases])
        candidate_vector = _vector([item.candidate for item in cases])
        deltas = MappingProxyType(_deltas(parent_vector, candidate_vector))
        reasons = _reasons(cases, parent_vector, candidate_vector, deltas, self._policy)
        finished = _utc(self._clock.now())
        status = ReplayStatus.PASSED if not reasons else ReplayStatus.FAILED
        document = _report_document(request, self._policy, status, reasons,
                                    parent_vector, candidate_vector, deltas, cases,
                                    started, finished)
        report = ReplayReport(
            request.replay_id, request.proposal.proposal_id,
            request.dataset.manifest.manifest_digest, self._policy.policy_version,
            status, reasons, parent_vector, candidate_vector, deltas, tuple(cases),
            started, finished, _digest(document),
        )
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT request_digest FROM dna_replay_run WHERE replay_id=?",
                (request.replay_id,),
            )
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise DnaReplayError("replay ID already exists with another request")
                return await self._load(transaction, request.replay_id)
            await transaction.execute(
                "INSERT INTO dna_replay_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.replay_id, request.proposal.proposal_id,
                 request.dataset.manifest.dataset_id, request.dataset.manifest.version,
                 request.dataset.manifest.manifest_digest, self._policy.policy_version,
                 request_digest, status.value, len(cases),
                 sum(item.context.fault is not FaultScenario.NONE for item in cases),
                 _json(document), report.report_digest, _time(started), _time(finished),
                 request.correlation_id),
            )
            await transaction.executemany(
                "INSERT INTO dna_replay_case VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(_case_values(request.replay_id, item) for item in cases),
            )
        return report

    async def get(self, replay_id: str) -> ReplayReport:
        async with self._database.transaction() as transaction:
            return await self._load(transaction, replay_id)

    async def _validate_sources(
        self, transaction: SQLiteTransaction, request: ReplayRequest,
        selected: Sequence[ExperienceSample],
    ) -> None:
        proposal = await transaction.fetch_one(
            """SELECT proposal_digest,dataset_manifest_digest,candidate_document_json,
                      base_dna_id,base_version,base_content_digest
               FROM dna_candidate_proposal WHERE proposal_id=?""",
            (request.proposal.proposal_id,),
        )
        if (proposal is None
                or str(proposal["proposal_digest"]) != request.proposal.proposal_digest
                or str(proposal["dataset_manifest_digest"])
                != request.dataset.manifest.manifest_digest
                or json.loads(str(proposal["candidate_document_json"]))
                != request.proposal.candidate.to_document()
                or (str(proposal["base_dna_id"]), str(proposal["base_version"]),
                    str(proposal["base_content_digest"]))
                != (request.parent.dna_id, request.parent.version,
                    request.parent.content_digest)):
            raise DnaReplayError("replay candidate proposal is missing or does not match")
        if request.proposal.candidate.status is not DnaStatus.CANDIDATE:
            raise DnaReplayError("sandbox replay only accepts Candidate DNA")
        base = _base_parent(request)
        parent_row = await transaction.fetch_one(
            """SELECT document_json FROM dna_definition
               WHERE dna_id=? AND version=? AND content_digest=?""",
            (base.dna_id, base.version, base.content_digest),
        )
        if parent_row is None:
            raise DnaReplayError("replay parent DNA is missing or does not match")
        try:
            persisted_parent = DnaDefinition.from_document(
                json.loads(str(parent_row["document_json"])),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DnaReplayError(
                "replay parent DNA is missing or does not match",
            ) from error
        # Governance envelope fields (for example CANDIDATE -> VALIDATED) may
        # change without changing the executable DNA pinned by a replay.
        if (persisted_parent.dna_id, persisted_parent.version,
                persisted_parent.content_digest, persisted_parent.workflow) != (
                    base.dna_id, base.version, base.content_digest, base.workflow):
            raise DnaReplayError("replay parent DNA is missing or does not match")
        if not self._policy.allow_non_replayable_simulation:
            for dna in (base, request.proposal.candidate):
                if _has_non_replayable(dna):
                    raise DnaReplayError("NON_REPLAYABLE DNA requires an explicit simulator policy")
        dataset = await transaction.fetch_one(
            """SELECT manifest_digest FROM dna_experience_dataset
               WHERE dataset_id=? AND version=?""",
            (request.dataset.manifest.dataset_id, request.dataset.manifest.version),
        )
        if dataset is None or str(dataset["manifest_digest"]) != request.dataset.manifest.manifest_digest:
            raise DnaReplayError("replay dataset is missing or manifest does not match")
        for sample in selected:
            row = await transaction.fetch_one(
                """SELECT sample_digest FROM dna_experience_sample
                   WHERE dataset_id=? AND dataset_version=? AND sample_id=?""",
                (request.dataset.manifest.dataset_id, request.dataset.manifest.version,
                 sample.sample_id),
            )
            if row is None or str(row["sample_digest"]) != sample.sample_digest:
                raise DnaReplayError("replay sample is missing or digest does not match")

    async def _load(self, transaction: SQLiteTransaction, replay_id: str) -> ReplayReport:
        row = await transaction.fetch_one(
            "SELECT report_json,report_digest FROM dna_replay_run WHERE replay_id=?",
            (replay_id,),
        )
        if row is None:
            raise DnaReplayError(f"sandbox replay not found: {replay_id}")
        document = json.loads(str(row["report_json"]))
        if _digest(document) != str(row["report_digest"]):
            raise DnaReplayError("sandbox replay report digest mismatch")
        cases = await transaction.fetch_all(
            "SELECT * FROM dna_replay_case WHERE replay_id=? ORDER BY ordinal", (replay_id,)
        )
        return _report(document, cases, str(row["report_digest"]))


def _base_parent(request: ReplayRequest) -> DnaDefinition:
    parent = (request.proposal.candidate.parent_dna[0]
              if request.proposal.candidate.parent_dna else None)
    if (parent is None or (parent.dna_id, parent.version, parent.content_digest)
            != (request.parent.dna_id, request.parent.version, request.parent.content_digest)):
        raise DnaReplayError("Candidate DNA has no base parent")
    return request.parent


def _has_non_replayable(dna: DnaDefinition) -> bool:
    nodes = cast(Sequence[Mapping[str, object]], dna.workflow["nodes"])
    return any(
        node["type"] == "skill"
        and cast(Mapping[str, object], node["constraints"])["side_effect"] == "NON_REPLAYABLE"
        for node in nodes
    )


def _vector(values: Sequence[ReplayMeasurement]) -> ReplayVector:
    count = len(values)
    latencies = sorted(item.latency_ms for item in values)
    return ReplayVector(
        _mean([int(item.successful) for item in values], count),
        _mean([item.evidence_score for item in values], count),
        _mean([item.user_value_score for item in values], count),
        _mean([item.cost_minor for item in values], count),
        _mean([item.latency_ms for item in values], count),
        latencies[math.ceil(0.95 * count) - 1],
        _mean([int(item.stable) for item in values], count),
        _mean([int(bool(item.risk_violations)) for item in values], count),
    )


def _deltas(parent: ReplayVector, candidate: ReplayVector) -> dict[str, float]:
    return {
        "success_rate": round(candidate.success_rate - parent.success_rate, 6),
        "evidence_score": round(candidate.evidence_score - parent.evidence_score, 6),
        "user_value_score": round(candidate.user_value_score - parent.user_value_score, 6),
        "stability_rate": round(candidate.stability_rate - parent.stability_rate, 6),
        "risk_rate": round(candidate.risk_rate - parent.risk_rate, 6),
        "cost_increase_ratio": _increase(parent.average_cost_minor,
                                           candidate.average_cost_minor),
        "latency_increase_ratio": _increase(parent.average_latency_ms,
                                              candidate.average_latency_ms),
    }


def _increase(parent: float, candidate: float) -> float:
    if parent == 0:
        return 0.0 if candidate == 0 else 1_000_000_000.0
    return round((candidate - parent) / parent, 6)


def _reasons(
    cases: Sequence[ReplayCase], parent: ReplayVector, candidate: ReplayVector,
    deltas: Mapping[str, float], policy: ReplayPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if any(not item.parent_deterministic for item in cases):
        reasons.append("parent_nondeterministic")
    if any(not item.candidate_deterministic for item in cases):
        reasons.append("candidate_nondeterministic")
    for key, minimum, reason in (
        ("success_rate", policy.minimum_success_delta, "success_delta_below_threshold"),
        ("evidence_score", policy.minimum_evidence_delta, "evidence_delta_below_threshold"),
        ("user_value_score", policy.minimum_value_delta, "value_delta_below_threshold"),
        ("stability_rate", policy.minimum_stability_delta, "stability_delta_below_threshold"),
    ):
        if deltas[key] < minimum:
            reasons.append(reason)
    if deltas["cost_increase_ratio"] > policy.maximum_cost_increase_ratio:
        reasons.append("cost_increase_exceeded")
    if deltas["latency_increase_ratio"] > policy.maximum_latency_increase_ratio:
        reasons.append("latency_increase_exceeded")
    if candidate.risk_rate > policy.maximum_candidate_risk_rate:
        reasons.append("candidate_risk_exceeded")
    if candidate.success_rate < parent.success_rate and "success_delta_below_threshold" not in reasons:
        reasons.append("candidate_regressed_success")
    return tuple(reasons)


def _request_document(
    request: ReplayRequest, policy: ReplayPolicy, selected: Sequence[ExperienceSample],
) -> dict[str, object]:
    return {
        "replay_id": request.replay_id, "proposal_digest": request.proposal.proposal_digest,
        "parent_digest": request.parent.content_digest,
        "candidate_digest": request.proposal.candidate.content_digest,
        "dataset_manifest_digest": request.dataset.manifest.manifest_digest,
        "policy": {
            "policy_version": policy.policy_version, "repetitions": policy.repetitions,
            "minimum_cases": policy.minimum_cases,
            "minimum_success_delta": policy.minimum_success_delta,
            "minimum_evidence_delta": policy.minimum_evidence_delta,
            "minimum_value_delta": policy.minimum_value_delta,
            "minimum_stability_delta": policy.minimum_stability_delta,
            "maximum_cost_increase_ratio": policy.maximum_cost_increase_ratio,
            "maximum_latency_increase_ratio": policy.maximum_latency_increase_ratio,
            "maximum_candidate_risk_rate": policy.maximum_candidate_risk_rate,
            "included_splits": sorted(item.value for item in policy.included_splits),
            "allow_non_replayable_simulation": policy.allow_non_replayable_simulation,
        },
        "samples": [{"sample_id": item.sample_id, "sample_digest": item.sample_digest,
                     "fault": request.faults.get(item.sample_id, FaultScenario.NONE).value}
                    for item in selected],
    }


def _report_document(
    request: ReplayRequest, policy: ReplayPolicy, status: ReplayStatus,
    reasons: tuple[str, ...], parent: ReplayVector, candidate: ReplayVector,
    deltas: Mapping[str, float], cases: Sequence[ReplayCase], started: datetime,
    finished: datetime,
) -> dict[str, object]:
    return {
        "schema_version": "1.0", "replay_id": request.replay_id,
        "proposal_id": request.proposal.proposal_id,
        "dataset_manifest_digest": request.dataset.manifest.manifest_digest,
        "policy_version": policy.policy_version, "status": status.value,
        "reasons": list(reasons), "parent": parent.to_document(),
        "candidate": candidate.to_document(), "deltas": dict(deltas),
        "cases": [{"sample_id": item.sample_id, "case_digest": item.case_digest}
                  for item in cases],
        "started_at": _time(started), "finished_at": _time(finished),
    }


def _case_values(replay_id: str, value: ReplayCase) -> tuple[str | int, ...]:
    return (
        replay_id, value.sample_id, value.ordinal, value.split.value,
        _time(value.context.virtual_time), value.context.deterministic_seed,
        value.context.fault.value, _json(value.parent.to_document()),
        _json(value.candidate.to_document()), int(value.parent_deterministic),
        int(value.candidate_deterministic), value.case_digest,
    )


def _report(
    document: Mapping[str, object], rows: Sequence[sqlite3.Row], report_digest: str,
) -> ReplayReport:
    expected = {str(item["sample_id"]): str(item["case_digest"])
                for item in cast(Sequence[Mapping[str, object]], document["cases"])}
    cases = tuple(_case_from_row(row) for row in rows)
    if len(expected) != len(cases) or any(expected.get(item.sample_id) != item.case_digest
                                          for item in cases):
        raise DnaReplayError("sandbox replay case digest mismatch")
    return ReplayReport(
        str(document["replay_id"]), str(document["proposal_id"]),
        str(document["dataset_manifest_digest"]), str(document["policy_version"]),
        ReplayStatus(str(document["status"])), tuple(cast(Sequence[str], document["reasons"])),
        _vector_from_document(cast(Mapping[str, object], document["parent"])),
        _vector_from_document(cast(Mapping[str, object], document["candidate"])),
        MappingProxyType({str(key): _number(value) for key, value in
                          cast(Mapping[str, object], document["deltas"]).items()}),
        cases, _parse_time(str(document["started_at"])),
        _parse_time(str(document["finished_at"])), report_digest,
    )


def _case_from_row(row: sqlite3.Row) -> ReplayCase:
    context = ReplayContext(
        str(row["replay_id"]), str(row["sample_id"]),
        _parse_time(str(row["virtual_time"])), str(row["deterministic_seed"]),
        FaultScenario(str(row["fault"])),
    )
    parent = _measurement(json.loads(str(row["parent_measurement_json"])))
    candidate = _measurement(json.loads(str(row["candidate_measurement_json"])))
    case_document = {
        "sample_id": str(row["sample_id"]), "split": str(row["split"]),
        "virtual_time": _time(context.virtual_time), "seed": context.deterministic_seed,
        "fault": context.fault.value, "parent": parent.to_document(),
        "candidate": candidate.to_document(),
        "parent_deterministic": bool(row["parent_deterministic"]),
        "candidate_deterministic": bool(row["candidate_deterministic"]),
    }
    if _digest(case_document) != str(row["case_digest"]):
        raise DnaReplayError("sandbox replay case digest mismatch")
    return ReplayCase(
        str(row["sample_id"]), int(row["ordinal"]), DatasetSplit(str(row["split"])),
        context, parent, candidate, bool(row["parent_deterministic"]),
        bool(row["candidate_deterministic"]), str(row["case_digest"]),
    )


def _measurement(document: Mapping[str, object]) -> ReplayMeasurement:
    return ReplayMeasurement(
        bool(document["successful"]), _number(document["evidence_score"]),
        _number(document["user_value_score"]), _integer(document["cost_minor"]),
        _integer(document["latency_ms"]), bool(document["stable"]),
        tuple(cast(Sequence[str], document["risk_violations"])),
        str(document["output_digest"]),
    )


def _vector_from_document(document: Mapping[str, object]) -> ReplayVector:
    return ReplayVector(
        _number(document["success_rate"]), _number(document["evidence_score"]),
        _number(document["user_value_score"]), _number(document["average_cost_minor"]),
        _number(document["average_latency_ms"]), _integer(document["p95_latency_ms"]),
        _number(document["stability_rate"]), _number(document["risk_rate"]),
    )


def _mean(values: Sequence[int | float], count: int) -> float:
    return round(sum(values) / count, 6)


def _number(value: object) -> float:
    if not isinstance(value, int | float):
        raise DnaReplayError("persisted replay number is invalid")
    return float(value)


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise DnaReplayError("persisted replay integer is invalid")
    return value


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaReplayError("replay time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
