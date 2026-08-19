"""Tamper-evident lineage and decision explanations for evolved DNA."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock
from domain_sdk.dna import DnaDefinition


class DnaExplainError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExplainRequest:
    explanation_id: str
    dna_id: str
    version: str
    correlation_id: str

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[a-zA-Z0-9_.:-]{3,128}", self.explanation_id) is None
                or not self.dna_id or not self.version or not self.correlation_id):
            raise DnaExplainError("explanation request metadata is invalid")


@dataclass(frozen=True, slots=True)
class EvolutionExplanation:
    explanation_id: str
    dna_id: str
    version: str
    content_digest: str
    why: tuple[str, ...]
    document: Mapping[str, object]
    explained_at: datetime
    explanation_digest: str


class DnaLineageExplainer:
    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def explain(self, request: ExplainRequest) -> EvolutionExplanation:
        request_digest = _digest({"explanation_id": request.explanation_id,
                                  "dna_id": request.dna_id, "version": request.version})
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT request_digest FROM dna_explanation WHERE explanation_id=?",
                (request.explanation_id,),
            )
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise DnaExplainError("explanation ID already exists for another DNA")
                return await self._load(transaction, request.explanation_id)
            target, lineage = await _lineage(transaction, request.dna_id, request.version)
            generation = await _generation(transaction, target)
            proposal_id = None if generation is None else str(generation["proposal_id"])
            fitness = await _fitness(transaction, target)
            replays = await _replays(transaction, proposal_id)
            selections = await _selections(transaction, proposal_id)
            promotions = await _promotions(transaction, proposal_id)
            transitions = await _transitions(transaction, target)
            why = _why(generation, replays, selections, promotions, transitions)
            explained_at = _utc(self._clock.now())
            document: dict[str, object] = {
                "explanation_id": request.explanation_id,
                "target": _identity(target), "lineage": lineage,
                "generation": generation, "fitness": fitness, "replays": replays,
                "selections": selections, "promotions": promotions,
                "transitions": transitions, "why": list(why),
                "explained_at": _time(explained_at),
            }
            explanation_digest = _digest(document)
            await transaction.execute(
                "INSERT INTO dna_explanation VALUES (?,?,?,?,?,?,?,?,?)",
                (request.explanation_id, request.dna_id, request.version,
                 target.content_digest, request_digest, _json(document), explanation_digest,
                 _time(explained_at), request.correlation_id),
            )
        return EvolutionExplanation(
            request.explanation_id, request.dna_id, request.version, target.content_digest,
            why, MappingProxyType(document), explained_at, explanation_digest,
        )

    async def get(self, explanation_id: str) -> EvolutionExplanation:
        async with self._database.transaction() as transaction:
            return await self._load(transaction, explanation_id)

    async def _load(self, transaction: SQLiteTransaction,
                    explanation_id: str) -> EvolutionExplanation:
        row = await transaction.fetch_one(
            "SELECT * FROM dna_explanation WHERE explanation_id=?", (explanation_id,),
        )
        if row is None:
            raise DnaExplainError(f"DNA explanation not found: {explanation_id}")
        document = cast(dict[str, object], json.loads(str(row["document_json"])))
        digest = str(row["explanation_digest"])
        if _digest(document) != digest:
            raise DnaExplainError("DNA explanation digest mismatch")
        target = cast(Mapping[str, object], document["target"])
        if (str(target["dna_id"]), str(target["version"]), str(target["content_digest"])) != (
            str(row["dna_id"]), str(row["dna_version"]), str(row["content_digest"]),
        ):
            raise DnaExplainError("DNA explanation target mismatch")
        return EvolutionExplanation(
            explanation_id, str(row["dna_id"]), str(row["dna_version"]),
            str(row["content_digest"]),
            tuple(cast(Sequence[str], document["why"])), MappingProxyType(document),
            _parse_time(str(row["explained_at"])), digest,
        )


async def _lineage(transaction: SQLiteTransaction, dna_id: str,
                   version: str) -> tuple[DnaDefinition, list[dict[str, object]]]:
    pending = [(dna_id, version, 0)]
    seen: set[tuple[str, str]] = set()
    definitions: list[tuple[DnaDefinition, int]] = []
    while pending:
        identity, item_version, depth = pending.pop(0)
        key = (identity, item_version)
        if key in seen:
            raise DnaExplainError("DNA lineage contains a cycle or duplicate ancestry")
        if depth > 32:
            raise DnaExplainError("DNA lineage exceeds explanation depth")
        seen.add(key)
        row = await transaction.fetch_one(
            "SELECT document_json,content_digest FROM dna_definition WHERE dna_id=? AND version=?",
            key,
        )
        if row is None:
            raise DnaExplainError(f"DNA lineage member is missing: {identity}@{item_version}")
        try:
            dna = DnaDefinition.from_document(json.loads(str(row["document_json"])))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DnaExplainError("DNA lineage document is invalid") from error
        if dna.content_digest != str(row["content_digest"]):
            raise DnaExplainError("DNA lineage content digest mismatch")
        definitions.append((dna, depth))
        for parent in dna.parent_dna:
            persisted = await transaction.fetch_one(
                """SELECT content_digest FROM dna_definition
                   WHERE dna_id=? AND version=?""", (parent.dna_id, parent.version),
            )
            if persisted is None or str(persisted["content_digest"]) != parent.content_digest:
                raise DnaExplainError("DNA lineage parent reference does not match storage")
            pending.append((parent.dna_id, parent.version, depth + 1))
    target = definitions[0][0]
    return target, [(_identity(dna) | {"depth": depth}) for dna, depth in definitions]


async def _generation(transaction: SQLiteTransaction,
                      target: DnaDefinition) -> dict[str, object] | None:
    row = await transaction.fetch_one(
        """SELECT * FROM dna_candidate_proposal
           WHERE candidate_dna_id=? AND candidate_content_digest=?""",
        (target.dna_id, target.content_digest),
    )
    if row is None:
        return None
    candidate_document = json.loads(str(row["candidate_document_json"]))
    try:
        generated_candidate = DnaDefinition.from_document(candidate_document)
    except (TypeError, ValueError) as error:
        raise DnaExplainError("generation Candidate document is invalid") from error
    if (generated_candidate.dna_id, generated_candidate.version,
            generated_candidate.content_digest, generated_candidate.workflow) != (
                target.dna_id, target.version, target.content_digest, target.workflow):
        raise DnaExplainError("generation Candidate document does not match DNA")
    operations = json.loads(str(row["operations_json"]))
    proposal_document = {
        "proposal_id": str(row["proposal_id"]), "mode": str(row["mode"]),
        "candidate": candidate_document, "base_digest": str(row["base_content_digest"]),
        "donor_digest": None if row["donor_content_digest"] is None
        else str(row["donor_content_digest"]),
        "dataset_manifest_digest": str(row["dataset_manifest_digest"]),
        "policy_version": str(row["policy_version"]), "hypothesis": str(row["hypothesis"]),
        "operations": operations,
    }
    if _digest(proposal_document) != str(row["proposal_digest"]):
        raise DnaExplainError("generation proposal digest mismatch")
    return {"proposal_id": str(row["proposal_id"]), "mode": str(row["mode"]),
            "hypothesis": str(row["hypothesis"]), "operations": operations,
            "dataset": {"dataset_id": str(row["dataset_id"]),
                        "version": str(row["dataset_version"]),
                        "manifest_digest": str(row["dataset_manifest_digest"])},
            "policy_version": str(row["policy_version"]),
            "proposal_digest": str(row["proposal_digest"])}


async def _fitness(transaction: SQLiteTransaction,
                   target: DnaDefinition) -> list[dict[str, object]]:
    rows = await transaction.fetch_all(
        """SELECT * FROM dna_fitness_snapshot WHERE dna_id=? AND version=?
           ORDER BY window_id""", (target.dna_id, target.version),
    )
    return [dict(row) for row in rows]


async def _replays(transaction: SQLiteTransaction,
                   proposal_id: str | None) -> list[dict[str, object]]:
    if proposal_id is None:
        return []
    rows = await transaction.fetch_all(
        "SELECT * FROM dna_replay_run WHERE proposal_id=? ORDER BY finished_at", (proposal_id,),
    )
    result = []
    for row in rows:
        report = json.loads(str(row["report_json"]))
        if _digest(report) != str(row["report_digest"]):
            raise DnaExplainError("Replay report digest mismatch")
        result.append({"replay_id": str(row["replay_id"]), "status": str(row["status"]),
                       "policy_version": str(row["policy_version"]),
                       "report_digest": str(row["report_digest"]),
                       "reasons": report["reasons"], "deltas": report["deltas"]})
    return result


async def _selections(transaction: SQLiteTransaction,
                      proposal_id: str | None) -> list[dict[str, object]]:
    if proposal_id is None:
        return []
    rows = await transaction.fetch_all(
        """SELECT m.*,r.policy_version,r.report_json,r.report_digest
           FROM dna_selection_member m JOIN dna_selection_run r USING(selection_id)
           WHERE m.proposal_id=? ORDER BY r.selected_at""", (proposal_id,),
    )
    result = []
    for row in rows:
        report = json.loads(str(row["report_json"]))
        if _digest(report) != str(row["report_digest"]):
            raise DnaExplainError("Selection report digest mismatch")
        expected = {str(item["proposal_id"]): str(item["member_digest"])
                    for item in cast(Sequence[Mapping[str, object]], report["members"])}
        member_document = {
            "proposal_id": str(row["proposal_id"]), "replay_id": str(row["replay_id"]),
            "content_digest": str(row["content_digest"]),
            "disposition": str(row["disposition"]), "pareto_rank": row["pareto_rank"],
            "novelty_score": float(row["novelty_score"]),
            "vector": json.loads(str(row["vector_json"])),
            "reasons": json.loads(str(row["reasons_json"])),
        }
        if (_digest(member_document) != str(row["member_digest"])
                or expected.get(str(row["proposal_id"])) != str(row["member_digest"])):
            raise DnaExplainError("Selection member digest mismatch")
        result.append({"selection_id": str(row["selection_id"]),
                       "disposition": str(row["disposition"]),
                       "pareto_rank": row["pareto_rank"],
                       "novelty_score": float(row["novelty_score"]),
                       "reasons": json.loads(str(row["reasons_json"])),
                       "policy_version": str(row["policy_version"]),
                       "member_digest": str(row["member_digest"])})
    return result


async def _promotions(transaction: SQLiteTransaction,
                      proposal_id: str | None) -> list[dict[str, object]]:
    if proposal_id is None:
        return []
    campaigns = await transaction.fetch_all(
        "SELECT * FROM dna_promotion_campaign WHERE proposal_id=? ORDER BY created_at",
        (proposal_id,),
    )
    result = []
    for campaign in campaigns:
        events = await transaction.fetch_all(
            """SELECT from_stage,to_stage,reason,campaign_revision,occurred_at
               FROM dna_promotion_event WHERE campaign_id=? ORDER BY rowid""",
            (campaign["campaign_id"],),
        )
        observations = await transaction.fetch_all(
            """SELECT observation_id,campaign_id,stage,successful,stable,risk_violations_json,
                      duplicate_side_effect,permission_expanded,recovery_failed,observed_at,
                      observation_digest
               FROM dna_promotion_observation WHERE campaign_id=? ORDER BY observed_at""",
            (campaign["campaign_id"],),
        )
        for observation in observations:
            observation_document = {
                "observation_id": str(observation["observation_id"]),
                "campaign_id": str(observation["campaign_id"]),
                "stage": str(observation["stage"]),
                "successful": bool(observation["successful"]),
                "stable": bool(observation["stable"]),
                "risk_violations": json.loads(str(observation["risk_violations_json"])),
                "duplicate_side_effect": bool(observation["duplicate_side_effect"]),
                "permission_expanded": bool(observation["permission_expanded"]),
                "recovery_failed": bool(observation["recovery_failed"]),
                "observed_at": str(observation["observed_at"]),
            }
            if _digest(observation_document) != str(observation["observation_digest"]):
                raise DnaExplainError("Promotion observation digest mismatch")
        result.append({"campaign_id": str(campaign["campaign_id"]),
                       "stage": str(campaign["stage"]),
                       "baseline_version": campaign["baseline_version"],
                       "policy_version": str(campaign["policy_version"]),
                       "events": [dict(row) for row in events],
                       "observations": [dict(row) for row in observations]})
    return result


async def _transitions(transaction: SQLiteTransaction,
                       target: DnaDefinition) -> list[dict[str, object]]:
    rows = await transaction.fetch_all(
        """SELECT from_status,to_status,reason,from_revision,to_revision,occurred_at
           FROM dna_transition WHERE dna_id=? AND version=? ORDER BY rowid""",
        (target.dna_id, target.version),
    )
    return [dict(row) for row in rows]


def _why(generation: Mapping[str, object] | None, replays: Sequence[Mapping[str, object]],
         selections: Sequence[Mapping[str, object]], promotions: Sequence[Mapping[str, object]],
         transitions: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    reasons: list[str] = []
    if generation is None:
        reasons.append("registered_or_composed_without_H06_proposal")
    else:
        reasons.append(f"generated:{generation['mode']}:{generation['hypothesis']}")
    reasons.extend(f"replay:{item['status']}" for item in replays)
    reasons.extend(f"selection:{item['disposition']}" for item in selections)
    for campaign in promotions:
        reasons.append(f"promotion:{campaign['stage']}")
        events = cast(Sequence[Mapping[str, object]], campaign["events"])
        if events:
            reasons.append(f"promotion_last_reason:{events[-1]['reason']}")
    if not promotions and transitions:
        reasons.append(f"registry_status:{transitions[-1]['to_status']}")
    return tuple(reasons)


def _identity(dna: DnaDefinition) -> dict[str, object]:
    return {"dna_id": dna.dna_id, "version": dna.version, "status": dna.status.value,
            "content_digest": dna.content_digest, "envelope_digest": dna.envelope_digest,
            "parents": [item.to_document() for item in dna.parent_dna]}


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaExplainError("explanation time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
