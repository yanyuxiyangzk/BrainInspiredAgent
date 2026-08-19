"""Auditable hard-gated Pareto and diversity selection for Candidate DNA."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock
from domain_sdk.dna_candidates import CandidateProposal
from domain_sdk.dna_replay import ReplayReport, ReplayStatus, ReplayVector


class DnaSelectionError(ValueError):
    pass


class SelectionDisposition(StrEnum):
    SELECTED = "SELECTED"
    DOMINATED = "DOMINATED"
    DUPLICATE = "DUPLICATE"
    HARD_REJECTED = "HARD_REJECTED"
    CAPACITY = "CAPACITY"


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    policy_version: str
    maximum_survivors: int
    minimum_population: int = 2
    minimum_novelty: float = 0.0

    def __post_init__(self) -> None:
        if not self.policy_version or self.maximum_survivors < 1 or self.minimum_population < 1:
            raise DnaSelectionError("selection policy identifiers and counts are invalid")
        if not 0 <= self.minimum_novelty <= 1:
            raise DnaSelectionError("selection novelty threshold is invalid")


@dataclass(frozen=True, slots=True)
class PopulationCandidate:
    proposal: CandidateProposal
    replay: ReplayReport


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    selection_id: str
    candidates: tuple[PopulationCandidate, ...]
    correlation_id: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_.:-]{3,128}", self.selection_id) is None \
                or not self.correlation_id:
            raise DnaSelectionError("selection request metadata is invalid")
        if not self.candidates:
            raise DnaSelectionError("selection population is empty")


@dataclass(frozen=True, slots=True)
class SelectionMember:
    proposal_id: str
    replay_id: str
    content_digest: str
    disposition: SelectionDisposition
    pareto_rank: int | None
    novelty_score: float
    vector: ReplayVector
    reasons: tuple[str, ...]
    member_digest: str


@dataclass(frozen=True, slots=True)
class SelectionReport:
    selection_id: str
    policy_version: str
    members: tuple[SelectionMember, ...]
    selected_proposal_ids: tuple[str, ...]
    selected_at: datetime
    report_digest: str


class DnaPopulationSelector:
    def __init__(self, database: SQLiteDatabase, clock: Clock, policy: SelectionPolicy) -> None:
        self._database = database
        self._clock = clock
        self._policy = policy

    async def select(self, request: SelectionRequest) -> SelectionReport:
        if len(request.candidates) < self._policy.minimum_population:
            raise DnaSelectionError("selection population is below minimum")
        if len({item.proposal.proposal_id for item in request.candidates}) \
                != len(request.candidates):
            raise DnaSelectionError("selection proposal IDs must be unique")
        request_document = _request_document(request, self._policy)
        request_digest = _digest(request_document)
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT request_digest FROM dna_selection_run WHERE selection_id=?",
                (request.selection_id,),
            )
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise DnaSelectionError("selection ID already exists with another request")
                return await self._load(transaction, request.selection_id)
            await self._validate_sources(transaction, request.candidates)

        members = _select(request.candidates, self._policy)
        selected_at = _utc(self._clock.now())
        selected = tuple(item.proposal_id for item in members
                         if item.disposition is SelectionDisposition.SELECTED)
        document = _report_document(request.selection_id, self._policy.policy_version,
                                    members, selected, selected_at)
        report = SelectionReport(request.selection_id, self._policy.policy_version, members,
                                 selected, selected_at, _digest(document))
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "INSERT INTO dna_selection_run VALUES (?,?,?,?,?,?,?,?,?)",
                (request.selection_id, self._policy.policy_version, request_digest,
                 len(members), len(selected), _json(document), report.report_digest,
                 _time(selected_at), request.correlation_id),
            )
            await transaction.executemany(
                "INSERT INTO dna_selection_member VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(_member_values(request.selection_id, item) for item in members),
            )
        return report

    async def get(self, selection_id: str) -> SelectionReport:
        async with self._database.transaction() as transaction:
            return await self._load(transaction, selection_id)

    async def _validate_sources(
        self, transaction: SQLiteTransaction, candidates: Sequence[PopulationCandidate],
    ) -> None:
        for item in candidates:
            proposal = await transaction.fetch_one(
                """SELECT proposal_digest,candidate_content_digest FROM dna_candidate_proposal
                   WHERE proposal_id=?""", (item.proposal.proposal_id,),
            )
            if (proposal is None
                    or str(proposal["proposal_digest"]) != item.proposal.proposal_digest
                    or str(proposal["candidate_content_digest"])
                    != item.proposal.candidate.content_digest):
                raise DnaSelectionError("selection Candidate proposal does not match storage")
            replay = await transaction.fetch_one(
                """SELECT proposal_id,status,report_digest,report_json FROM dna_replay_run
                   WHERE replay_id=?""", (item.replay.replay_id,),
            )
            if (replay is None or str(replay["proposal_id"]) != item.proposal.proposal_id
                    or str(replay["status"]) != item.replay.status.value
                    or str(replay["report_digest"]) != item.replay.report_digest
                    or _digest(json.loads(str(replay["report_json"])))
                    != item.replay.report_digest):
                raise DnaSelectionError("selection Replay report does not match storage")

    async def _load(self, transaction: SQLiteTransaction, selection_id: str) -> SelectionReport:
        row = await transaction.fetch_one(
            "SELECT report_json,report_digest FROM dna_selection_run WHERE selection_id=?",
            (selection_id,),
        )
        if row is None:
            raise DnaSelectionError(f"DNA selection not found: {selection_id}")
        document = json.loads(str(row["report_json"]))
        digest = str(row["report_digest"])
        if _digest(document) != digest:
            raise DnaSelectionError("DNA selection report digest mismatch")
        rows = await transaction.fetch_all(
            "SELECT * FROM dna_selection_member WHERE selection_id=? ORDER BY proposal_id",
            (selection_id,),
        )
        members = tuple(_member_from_row(item) for item in rows)
        expected = {str(item["proposal_id"]): str(item["member_digest"])
                    for item in cast(Sequence[Mapping[str, object]], document["members"])}
        if len(expected) != len(members) or any(
                expected.get(item.proposal_id) != item.member_digest for item in members):
            raise DnaSelectionError("DNA selection member digest mismatch")
        return SelectionReport(
            str(document["selection_id"]), str(document["policy_version"]), members,
            tuple(cast(Sequence[str], document["selected_proposal_ids"])),
            _parse_time(str(document["selected_at"])), digest,
        )


def _select(candidates: Sequence[PopulationCandidate], policy: SelectionPolicy) \
        -> tuple[SelectionMember, ...]:
    signatures = {item.proposal.proposal_id: _signature(item.proposal) for item in candidates}
    novelty = {item.proposal.proposal_id: _novelty(item, candidates, signatures)
               for item in candidates}
    canonical: dict[str, PopulationCandidate] = {}
    duplicates: set[str] = set()
    eligible: list[PopulationCandidate] = []
    rejected: set[str] = set()
    for item in sorted(candidates, key=lambda value: value.proposal.proposal_id):
        digest = item.proposal.candidate.content_digest
        if digest in canonical:
            duplicates.add(item.proposal.proposal_id)
        else:
            canonical[digest] = item
            if item.replay.status is ReplayStatus.PASSED and not item.replay.reasons:
                eligible.append(item)
            else:
                rejected.add(item.proposal.proposal_id)
    front = [item for item in eligible if not any(
        other is not item and _dominates(other.replay.candidate, item.replay.candidate)
        for other in eligible
    )]
    ranked = sorted(front, key=lambda item: (
        -novelty[item.proposal.proposal_id], -item.replay.candidate.success_rate,
        -item.replay.candidate.evidence_score, item.proposal.proposal_id,
    ))
    qualified = [item for item in ranked
                 if novelty[item.proposal.proposal_id] >= policy.minimum_novelty]
    selected = {item.proposal.proposal_id
                for item in qualified[:policy.maximum_survivors]}
    result: list[SelectionMember] = []
    for item in sorted(candidates, key=lambda value: value.proposal.proposal_id):
        proposal_id = item.proposal.proposal_id
        reasons: tuple[str, ...]
        if proposal_id in duplicates:
            disposition, rank, reasons = SelectionDisposition.DUPLICATE, None, ("duplicate_content",)
        elif proposal_id in rejected:
            disposition, rank, reasons = SelectionDisposition.HARD_REJECTED, None, \
                (("replay_failed",) + item.replay.reasons)
        elif item not in front:
            disposition, rank, reasons = SelectionDisposition.DOMINATED, 1, ("pareto_dominated",)
        elif proposal_id in selected:
            disposition, rank, reasons = SelectionDisposition.SELECTED, 0, ()
        else:
            disposition, rank = SelectionDisposition.CAPACITY, 0
            reasons = (("novelty_below_threshold",) if novelty[proposal_id] < policy.minimum_novelty
                       else ("survivor_capacity",))
        member_document = _member_document(
            proposal_id, item.replay.replay_id, item.proposal.candidate.content_digest,
            disposition, rank, novelty[proposal_id], item.replay.candidate, reasons,
        )
        result.append(SelectionMember(
            proposal_id, item.replay.replay_id, item.proposal.candidate.content_digest,
            disposition, rank, novelty[proposal_id], item.replay.candidate, reasons,
            _digest(member_document),
        ))
    if not any(item.disposition is SelectionDisposition.SELECTED for item in result):
        raise DnaSelectionError("selection produced no eligible survivors")
    return tuple(result)


def _dominates(left: ReplayVector, right: ReplayVector) -> bool:
    left_values = (left.success_rate, left.evidence_score, left.user_value_score,
                   left.stability_rate, -left.average_cost_minor,
                   -left.average_latency_ms, -left.p95_latency_ms, -left.risk_rate)
    right_values = (right.success_rate, right.evidence_score, right.user_value_score,
                    right.stability_rate, -right.average_cost_minor,
                    -right.average_latency_ms, -right.p95_latency_ms, -right.risk_rate)
    return all(a >= b for a, b in zip(left_values, right_values, strict=True)) \
        and any(a > b for a, b in zip(left_values, right_values, strict=True))


def _signature(proposal: CandidateProposal) -> frozenset[str]:
    return frozenset(_json(item.to_document()) for item in proposal.operations)


def _novelty(item: PopulationCandidate, population: Sequence[PopulationCandidate],
             signatures: Mapping[str, frozenset[str]]) -> float:
    own = signatures[item.proposal.proposal_id]
    others = [signatures[value.proposal.proposal_id] for value in population if value is not item]
    if not others:
        return 1.0
    similarities = [len(own & other) / len(own | other) if own | other else 1.0
                    for other in others]
    return round(1 - max(similarities), 6)


def _request_document(request: SelectionRequest, policy: SelectionPolicy) -> dict[str, object]:
    return {"selection_id": request.selection_id, "policy_version": policy.policy_version,
            "maximum_survivors": policy.maximum_survivors,
            "minimum_population": policy.minimum_population,
            "minimum_novelty": policy.minimum_novelty,
            "members": [{"proposal_digest": item.proposal.proposal_digest,
                         "replay_digest": item.replay.report_digest}
                        for item in request.candidates]}


def _member_document(proposal_id: str, replay_id: str, content_digest: str,
                     disposition: SelectionDisposition, rank: int | None, novelty: float,
                     vector: ReplayVector, reasons: Sequence[str]) -> dict[str, object]:
    return {"proposal_id": proposal_id, "replay_id": replay_id,
            "content_digest": content_digest, "disposition": disposition.value,
            "pareto_rank": rank, "novelty_score": novelty,
            "vector": vector.to_document(), "reasons": list(reasons)}


def _report_document(selection_id: str, policy_version: str,
                     members: Sequence[SelectionMember], selected: Sequence[str],
                     selected_at: datetime) -> dict[str, object]:
    return {"selection_id": selection_id, "policy_version": policy_version,
            "members": [{"proposal_id": item.proposal_id,
                         "member_digest": item.member_digest} for item in members],
            "selected_proposal_ids": list(selected), "selected_at": _time(selected_at)}


def _member_values(
    selection_id: str, item: SelectionMember,
) -> tuple[str | int | float | None, ...]:
    return (selection_id, item.proposal_id, item.replay_id, item.content_digest,
            item.disposition.value, item.pareto_rank, item.novelty_score,
            _json(item.vector.to_document()), _json(list(item.reasons)), item.member_digest)


def _member_from_row(row: sqlite3.Row) -> SelectionMember:
    vector_document = json.loads(str(row["vector_json"]))
    vector = ReplayVector(
        float(vector_document["success_rate"]), float(vector_document["evidence_score"]),
        float(vector_document["user_value_score"]),
        float(vector_document["average_cost_minor"]),
        float(vector_document["average_latency_ms"]), int(vector_document["p95_latency_ms"]),
        float(vector_document["stability_rate"]), float(vector_document["risk_rate"]),
    )
    rank = None if row["pareto_rank"] is None else int(row["pareto_rank"])
    reasons = tuple(cast(Sequence[str], json.loads(str(row["reasons_json"]))))
    document = _member_document(
        str(row["proposal_id"]), str(row["replay_id"]), str(row["content_digest"]),
        SelectionDisposition(str(row["disposition"])), rank, float(row["novelty_score"]),
        vector, reasons,
    )
    if _digest(document) != str(row["member_digest"]):
        raise DnaSelectionError("DNA selection member digest mismatch")
    return SelectionMember(
        str(row["proposal_id"]), str(row["replay_id"]), str(row["content_digest"]),
        SelectionDisposition(str(row["disposition"])), rank, float(row["novelty_score"]),
        vector, reasons, str(row["member_digest"]),
    )


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaSelectionError("selection time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
