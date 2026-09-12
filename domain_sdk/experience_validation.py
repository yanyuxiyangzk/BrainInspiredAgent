"""X-03: candidate-experience validation — replay consistency + anti-overfitting bounds.

对一组"声明相同"的 CANDIDATE 经验（X-02 产出，同一 ``conditions`` 与同一结局
方向、来自互异情景）做三道确定性门，全部通过才给 VALIDATED：

1. 防过拟合下界：支撑声明的互异 Episode 数 ≥ ``minimum_samples``；
2. 重放一致性：注入的 ``ReplayOracle`` 逐个重放——``successful`` 必须一致，
   ``quality`` 偏差不得超出 ``maximum_score_deviation``，一致率
   ≥ ``minimum_replay_agreement``；
3. 矛盾拒收：组内出现同声明、结局相反的样本时整组 ``CONTRADICTED``——
   矛盾是证据，不允许用多数票掩盖。

验证器不持久化；``ValidationVerdict`` 可序列化（``from_document`` 防篡改回读），
由调用方决定写入何种事实表。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from domain_sdk.experience_extraction import ExperienceCandidate

_MAX_DEVIATION = 1.0


class ExperienceValidationError(ValueError):
    pass


class VerdictStatus(StrEnum):
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    CONTRADICTED = "CONTRADICTED"


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    policy_version: str
    minimum_samples: int = 2
    minimum_replay_agreement: float = 0.8
    maximum_score_deviation: float = 0.15

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise ExperienceValidationError("policy version must not be empty")
        if self.minimum_samples < 1:
            raise ExperienceValidationError("policy minimum_samples must be positive")
        if not 0 < self.minimum_replay_agreement <= 1:
            raise ExperienceValidationError("policy replay agreement must be in (0, 1]")
        if not 0 <= self.maximum_score_deviation <= _MAX_DEVIATION:
            raise ExperienceValidationError("policy score deviation must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    experience_id: str
    episode_id: str
    successful: bool
    quality_score: float

    def __post_init__(self) -> None:
        if not 0 <= self.quality_score <= 1:
            raise ExperienceValidationError("replay quality_score must be between 0 and 1")

    def to_document(self) -> dict[str, object]:
        return {
            "experience_id": self.experience_id, "episode_id": self.episode_id,
            "successful": self.successful, "quality_score": self.quality_score,
        }


class ReplayOracle(Protocol):
    async def replay(self, candidate: ExperienceCandidate) -> ReplayObservation: ...


@dataclass(frozen=True, slots=True)
class ValidationVerdict:
    verdict_id: str
    status: VerdictStatus
    reasons: tuple[str, ...]
    members: tuple[ExperienceCandidate, ...]
    replay_observations: tuple[ReplayObservation, ...]
    replay_agreement: float
    distinct_episodes: int
    policy_version: str
    validated_at: datetime
    verdict_digest: str

    def to_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": "1.0",
            "verdict_id": self.verdict_id,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "members": [member.to_document() for member in self.members],
            "replay_observations": [
                observation.to_document() for observation in self.replay_observations
            ],
            "replay_agreement": self.replay_agreement,
            "distinct_episodes": self.distinct_episodes,
            "policy_version": self.policy_version,
            "validated_at": self.validated_at.isoformat().replace("+00:00", "Z"),
        }
        document["verdict_digest"] = _digest({
            key: value for key, value in document.items()
            if key not in {"verdict_digest", "validated_at"}
        })
        return document


class ExperienceValidator:
    """Deterministic three-gate validation over one claim group of candidates."""

    def __init__(
        self, validated_at: datetime, oracle: ReplayOracle, policy: ValidationPolicy,
    ) -> None:
        if validated_at.tzinfo is None or validated_at.utcoffset() is None:
            raise ExperienceValidationError("validated_at must be timezone-aware")
        self._validated_at = validated_at
        self._oracle = oracle
        self._policy = policy

    async def validate(
        self, members: tuple[ExperienceCandidate, ...], *, correlation_id: str,
    ) -> ValidationVerdict:
        del correlation_id
        if not members:
            raise ExperienceValidationError("claim group must not be empty")
        reasons: list[str] = []
        conditions = members[0].conditions
        successful = members[0].successful
        contradicting = tuple(
            member for member in members[1:]
            if dict(member.conditions) != dict(conditions) or member.successful is not successful
        )
        if contradicting:
            reasons.append(
                "contradiction: members share the claim but disagree on the outcome"
            )
        distinct = {member.evidence.episode_id for member in members}
        if len(distinct) < self._policy.minimum_samples:
            reasons.append(
                f"overfitting: {len(distinct)} independent episode(s) is below the "
                f"minimum of {self._policy.minimum_samples}"
            )
        observations: list[ReplayObservation] = []
        agreeing = 0
        for member in members:
            observation = await self._oracle.replay(member)
            observations.append(observation)
            consistent = (
                observation.successful is member.successful
                and observation.episode_id == member.evidence.episode_id
                and abs(observation.quality_score - member.quality_score)
                <= self._policy.maximum_score_deviation
            )
            agreeing += 1 if consistent else 0
        agreement = agreeing / len(members)
        if agreement < self._policy.minimum_replay_agreement:
            reasons.append(
                f"replay agreement {agreement:.2f} is below the minimum of "
                f"{self._policy.minimum_replay_agreement:.2f}"
            )
        if contradicting:
            status = VerdictStatus.CONTRADICTED
        elif reasons:
            status = VerdictStatus.REJECTED
        else:
            status = VerdictStatus.VALIDATED
        verdict_id = _digest({
            "policy": self._policy.policy_version,
            "member_ids": sorted(member.experience_id for member in members),
        })
        verdict = ValidationVerdict(
            verdict_id=verdict_id,
            status=status,
            reasons=tuple(reasons),
            members=members,
            replay_observations=tuple(observations),
            replay_agreement=agreement,
            distinct_episodes=len(distinct),
            policy_version=self._policy.policy_version,
            validated_at=self._validated_at,
            verdict_digest="",
        )
        document = verdict.to_document()
        return ValidationVerdict(
            verdict_id=verdict.verdict_id,
            status=verdict.status,
            reasons=verdict.reasons,
            members=verdict.members,
            replay_observations=verdict.replay_observations,
            replay_agreement=verdict.replay_agreement,
            distinct_episodes=verdict.distinct_episodes,
            policy_version=verdict.policy_version,
            validated_at=verdict.validated_at,
            verdict_digest=str(document["verdict_digest"]),
        )
