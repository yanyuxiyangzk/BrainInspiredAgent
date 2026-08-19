"""Governed Shadow/Canary promotion with hard stops and automatic rollback."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock, UuidGenerator
from domain_sdk.dna import DnaError, DnaStatus
from domain_sdk.dna_candidates import CandidateProposal
from domain_sdk.dna_repository import PersistentDnaRegistry


class DnaPromotionError(ValueError):
    pass


class PromotionStage(StrEnum):
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    STOPPED = "STOPPED"


class PromotionRoute(StrEnum):
    BASELINE = "BASELINE"
    SHADOW_MIRROR = "SHADOW_MIRROR"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    policy_version: str
    shadow_minimum_samples: int
    canary_minimum_samples: int
    shadow_minimum_duration: timedelta
    canary_minimum_duration: timedelta
    canary_fraction: float
    minimum_success_rate: float = 1.0
    minimum_stability_rate: float = 1.0
    maximum_risk_rate: float = 0.0

    def __post_init__(self) -> None:
        if (not self.policy_version or self.shadow_minimum_samples < 1
                or self.canary_minimum_samples < 1):
            raise DnaPromotionError("promotion policy identifiers and samples are invalid")
        if self.shadow_minimum_duration < timedelta(0) \
                or self.canary_minimum_duration < timedelta(0):
            raise DnaPromotionError("promotion durations must be non-negative")
        if not 0 < self.canary_fraction <= 1 or not all(0 <= value <= 1 for value in (
            self.minimum_success_rate, self.minimum_stability_rate, self.maximum_risk_rate,
        )):
            raise DnaPromotionError("promotion rates are invalid")


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    campaign_id: str
    selection_id: str
    proposal: CandidateProposal
    correlation_id: str

    def __post_init__(self) -> None:
        if (re.fullmatch(r"[a-zA-Z0-9_.:-]{3,128}", self.campaign_id) is None
                or not self.selection_id or not self.correlation_id):
            raise DnaPromotionError("promotion request metadata is invalid")


@dataclass(frozen=True, slots=True)
class PromotionObservation:
    observation_id: str
    campaign_id: str
    successful: bool
    stable: bool
    observed_at: datetime
    correlation_id: str
    risk_violations: tuple[str, ...] = ()
    duplicate_side_effect: bool = False
    permission_expanded: bool = False
    recovery_failed: bool = False

    def __post_init__(self) -> None:
        if not self.observation_id or not self.campaign_id or not self.correlation_id:
            raise DnaPromotionError("promotion observation metadata is invalid")
        if len(set(self.risk_violations)) != len(self.risk_violations):
            raise DnaPromotionError("promotion risk violations must be unique")
        _utc(self.observed_at)


@dataclass(frozen=True, slots=True)
class PromotionCampaign:
    campaign_id: str
    selection_id: str
    proposal_id: str
    dna_id: str
    dna_version: str
    content_digest: str
    policy_version: str
    stage: PromotionStage
    baseline_version: str | None
    stage_started_at: datetime
    revision: int


class DnaPromotionController:
    def __init__(
        self, database: SQLiteDatabase, registry: PersistentDnaRegistry, clock: Clock,
        identifiers: UuidGenerator, policy: PromotionPolicy,
    ) -> None:
        self._database = database
        self._registry = registry
        self._clock = clock
        self._identifiers = identifiers
        self._policy = policy

    async def start(self, request: PromotionRequest) -> PromotionCampaign:
        request_digest = _digest({
            "campaign_id": request.campaign_id, "selection_id": request.selection_id,
            "proposal_digest": request.proposal.proposal_digest,
            "policy_version": self._policy.policy_version,
        })
        existing = await self._database.fetch_one(
            "SELECT request_digest FROM dna_promotion_campaign WHERE campaign_id=?",
            (request.campaign_id,),
        )
        if existing is not None:
            if str(existing["request_digest"]) != request_digest:
                raise DnaPromotionError("campaign ID already exists with another request")
            return await self.get(request.campaign_id)
        async with self._database.transaction() as transaction:
            await self._validate_selected(transaction, request)
            active = await transaction.fetch_one(
                "SELECT version FROM dna_definition WHERE dna_id=? AND status='ACTIVE'",
                (request.proposal.candidate.dna_id,),
            )
        baseline = None if active is None else str(active["version"])
        try:
            record = await self._registry.get(
                request.proposal.candidate.dna_id, request.proposal.candidate.version,
            )
        except DnaError:
            record = await self._registry.register(
                request.proposal.candidate, correlation_id=request.correlation_id,
            )
        if record.dna.status is DnaStatus.CANDIDATE:
            record = await self._registry.transition(
                record.dna.dna_id, record.dna.version, DnaStatus.VALIDATED,
                expected_revision=record.revision, reason="H08 survivor validated",
                correlation_id=request.correlation_id,
            )
        if record.dna.status is DnaStatus.VALIDATED:
            record = await self._registry.transition(
                record.dna.dna_id, record.dna.version, DnaStatus.SHADOW,
                expected_revision=record.revision, reason="H09 shadow admitted",
                correlation_id=request.correlation_id,
            )
        if record.dna.status is not DnaStatus.SHADOW:
            raise DnaPromotionError("promotion Candidate is not eligible for Shadow")
        now = _utc(self._clock.now())
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "INSERT INTO dna_promotion_campaign VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.campaign_id, request.selection_id, request.proposal.proposal_id,
                 record.dna.dna_id, record.dna.version, record.dna.content_digest,
                 self._policy.policy_version, PromotionStage.SHADOW.value, baseline,
                 _time(now), 0, request_digest, _time(now), _time(now), request.correlation_id),
            )
            await self._event(transaction, request.campaign_id, None, PromotionStage.SHADOW,
                              "selected survivor entered Shadow", 0, request.correlation_id)
        return await self.get(request.campaign_id)

    async def observe(self, observation: PromotionObservation) -> PromotionCampaign:
        campaign = await self.get(observation.campaign_id)
        if campaign.stage not in {PromotionStage.SHADOW, PromotionStage.CANARY,
                                  PromotionStage.ACTIVE}:
            raise DnaPromotionError("promotion campaign is terminal")
        digest = _digest(_observation_document(observation, campaign.stage))
        duplicate = False
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT observation_digest FROM dna_promotion_observation WHERE observation_id=?",
                (observation.observation_id,),
            )
            if existing is not None:
                if str(existing["observation_digest"]) != digest:
                    raise DnaPromotionError("observation ID already exists with another payload")
                duplicate = True
            else:
                if observation.observed_at < campaign.stage_started_at:
                    raise DnaPromotionError("observation predates the current promotion stage")
                await transaction.execute(
                    "INSERT INTO dna_promotion_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (observation.observation_id, observation.campaign_id, campaign.stage.value,
                     int(observation.successful), int(observation.stable),
                     _json(list(observation.risk_violations)),
                     int(observation.duplicate_side_effect), int(observation.permission_expanded),
                     int(observation.recovery_failed), _time(observation.observed_at), digest,
                     observation.correlation_id),
                )
        if duplicate:
            return campaign
        hard_reason = _hard_stop(observation)
        if hard_reason is not None:
            return await self._stop(campaign, hard_reason, observation.correlation_id)
        return await self._evaluate(campaign, observation.correlation_id)

    async def kill(self, campaign_id: str, *, reason: str,
                   correlation_id: str) -> PromotionCampaign:
        if not reason.strip():
            raise DnaPromotionError("kill switch reason must not be empty")
        campaign = await self.get(campaign_id)
        if campaign.stage in {PromotionStage.ROLLED_BACK, PromotionStage.STOPPED}:
            return campaign
        return await self._stop(campaign, f"kill_switch:{reason}", correlation_id)

    async def get(self, campaign_id: str) -> PromotionCampaign:
        row = await self._database.fetch_one(
            "SELECT * FROM dna_promotion_campaign WHERE campaign_id=?", (campaign_id,),
        )
        if row is None:
            raise DnaPromotionError(f"promotion campaign not found: {campaign_id}")
        return PromotionCampaign(
            str(row["campaign_id"]), str(row["selection_id"]), str(row["proposal_id"]),
            str(row["dna_id"]), str(row["dna_version"]), str(row["content_digest"]),
            str(row["policy_version"]), PromotionStage(str(row["stage"])),
            None if row["baseline_version"] is None else str(row["baseline_version"]),
            _parse_time(str(row["stage_started_at"])), int(row["revision"]),
        )

    async def route(self, campaign_id: str, routing_key: str) -> PromotionRoute:
        if not routing_key:
            raise DnaPromotionError("promotion routing key must not be empty")
        campaign = await self.get(campaign_id)
        if campaign.stage is PromotionStage.SHADOW:
            return PromotionRoute.SHADOW_MIRROR
        if campaign.stage is PromotionStage.CANARY:
            bucket = int(hashlib.sha256(routing_key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
            return (PromotionRoute.CANARY if bucket < self._policy.canary_fraction
                    else PromotionRoute.BASELINE)
        if campaign.stage is PromotionStage.ACTIVE:
            return PromotionRoute.ACTIVE
        return PromotionRoute.BASELINE

    async def _evaluate(self, campaign: PromotionCampaign,
                        correlation_id: str) -> PromotionCampaign:
        rows = await self._database.fetch_all(
            """SELECT successful,stable,risk_violations_json FROM dna_promotion_observation
               WHERE campaign_id=? AND stage=? ORDER BY observed_at""",
            (campaign.campaign_id, campaign.stage.value),
        )
        if campaign.stage is PromotionStage.ACTIVE:
            return campaign
        minimum = (self._policy.shadow_minimum_samples if campaign.stage is PromotionStage.SHADOW
                   else self._policy.canary_minimum_samples)
        duration = (self._policy.shadow_minimum_duration if campaign.stage is PromotionStage.SHADOW
                    else self._policy.canary_minimum_duration)
        if len(rows) < minimum or _utc(self._clock.now()) < campaign.stage_started_at + duration:
            return campaign
        success_rate = sum(int(row["successful"]) for row in rows) / len(rows)
        stability_rate = sum(int(row["stable"]) for row in rows) / len(rows)
        risk_rate = sum(bool(json.loads(str(row["risk_violations_json"]))) for row in rows) / len(rows)
        if (success_rate < self._policy.minimum_success_rate
                or stability_rate < self._policy.minimum_stability_rate
                or risk_rate > self._policy.maximum_risk_rate):
            return await self._stop(campaign, "promotion_threshold_failed", correlation_id)
        record = await self._registry.get(campaign.dna_id, campaign.dna_version)
        if campaign.stage is PromotionStage.SHADOW:
            await self._registry.transition(
                campaign.dna_id, campaign.dna_version, DnaStatus.CANARY,
                expected_revision=record.revision, reason="Shadow gate passed",
                correlation_id=correlation_id,
            )
            return await self._change(campaign, PromotionStage.CANARY,
                                      "Shadow gate passed", correlation_id)
        await self._registry.activate(
            campaign.dna_id, campaign.dna_version, expected_revision=record.revision,
            reason="Canary gate passed", correlation_id=correlation_id,
        )
        return await self._change(campaign, PromotionStage.ACTIVE,
                                  "Canary gate passed", correlation_id)

    async def _stop(self, campaign: PromotionCampaign, reason: str,
                    correlation_id: str) -> PromotionCampaign:
        record = await self._registry.get(campaign.dna_id, campaign.dna_version)
        if campaign.stage is PromotionStage.ACTIVE and campaign.baseline_version is not None:
            target = await self._registry.get(campaign.dna_id, campaign.baseline_version)
            await self._registry.rollback(
                campaign.dna_id, campaign.baseline_version,
                expected_active_revision=record.revision,
                expected_target_revision=target.revision, reason=reason,
                correlation_id=correlation_id,
            )
            stage = PromotionStage.ROLLED_BACK
        else:
            if record.dna.status in {DnaStatus.SHADOW, DnaStatus.CANARY}:
                await self._registry.transition(
                    campaign.dna_id, campaign.dna_version, DnaStatus.RETIRED,
                    expected_revision=record.revision, reason=reason,
                    correlation_id=correlation_id,
                )
            stage = PromotionStage.STOPPED
        return await self._change(campaign, stage, reason, correlation_id)

    async def _change(self, campaign: PromotionCampaign, stage: PromotionStage,
                      reason: str, correlation_id: str) -> PromotionCampaign:
        now = _utc(self._clock.now())
        revision = campaign.revision + 1
        async with self._database.transaction() as transaction:
            cursor = await transaction.execute(
                """UPDATE dna_promotion_campaign SET stage=?,stage_started_at=?,revision=?,
                          updated_at=? WHERE campaign_id=? AND revision=?""",
                (stage.value, _time(now), revision, _time(now), campaign.campaign_id,
                 campaign.revision),
            )
            if cursor.rowcount != 1:
                raise DnaPromotionError("promotion campaign revision conflict")
            await self._event(transaction, campaign.campaign_id, campaign.stage, stage,
                              reason, revision, correlation_id)
        return await self.get(campaign.campaign_id)

    async def _validate_selected(self, transaction: SQLiteTransaction,
                                 request: PromotionRequest) -> None:
        row = await transaction.fetch_one(
            """SELECT disposition,content_digest FROM dna_selection_member
               WHERE selection_id=? AND proposal_id=?""",
            (request.selection_id, request.proposal.proposal_id),
        )
        if (row is None or str(row["disposition"]) != "SELECTED"
                or str(row["content_digest"]) != request.proposal.candidate.content_digest):
            raise DnaPromotionError("only an H08 selected Candidate can be promoted")
        proposal = await transaction.fetch_one(
            "SELECT proposal_digest FROM dna_candidate_proposal WHERE proposal_id=?",
            (request.proposal.proposal_id,),
        )
        if proposal is None or str(proposal["proposal_digest"]) != request.proposal.proposal_digest:
            raise DnaPromotionError("promotion Candidate proposal does not match storage")

    async def _event(self, transaction: SQLiteTransaction, campaign_id: str,
                     previous: PromotionStage | None, stage: PromotionStage, reason: str,
                     revision: int, correlation_id: str) -> None:
        await transaction.execute(
            "INSERT INTO dna_promotion_event VALUES (?,?,?,?,?,?,?,?)",
            (str(self._identifiers.new()), campaign_id,
             None if previous is None else previous.value, stage.value, reason, revision,
             _time(self._clock.now()), correlation_id),
        )


def _hard_stop(observation: PromotionObservation) -> str | None:
    if observation.duplicate_side_effect:
        return "duplicate_side_effect"
    if observation.permission_expanded:
        return "permission_expanded"
    if observation.recovery_failed:
        return "recovery_failed"
    if observation.risk_violations:
        return "risk_violation"
    return None


def _observation_document(observation: PromotionObservation,
                          stage: PromotionStage) -> dict[str, object]:
    return {"observation_id": observation.observation_id,
            "campaign_id": observation.campaign_id, "stage": stage.value,
            "successful": observation.successful, "stable": observation.stable,
            "risk_violations": list(observation.risk_violations),
            "duplicate_side_effect": observation.duplicate_side_effect,
            "permission_expanded": observation.permission_expanded,
            "recovery_failed": observation.recovery_failed,
            "observed_at": _time(observation.observed_at)}


def _digest(document: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(document).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DnaPromotionError("promotion time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
