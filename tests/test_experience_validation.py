"""X-03 tests: candidate-experience validation (replay consistency + anti-overfitting).

``domain_sdk.experience_validation.ExperienceValidator`` 对一组声明相同的
CANDIDATE 经验（X-02 产出）做三道门：

1. 防过拟合下界——支撑同一声明的互异 Episode 数不得低于策略下界；
2. 重放一致性——注入的 ReplayOracle 逐个重放，successful 必须一致、
   quality 偏差不得超出容忍度，一致率达标才可通过；
3. 矛盾拒收——同声明但结局相反的样本存在时整组 CONTRADICTED。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from domain_sdk.experience_extraction import ExperienceCandidate
from domain_sdk.experience_validation import (
    ExperienceValidationError,
    ExperienceValidator,
    ReplayObservation,
    ReplayOracle,
    ValidationPolicy,
    VerdictStatus,
)
from domain_sdk.workflow_patch import digest_document

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def candidate(episode_id: str, *, successful: bool = True, quality: float = 0.8) -> ExperienceCandidate:
    """Build a minimal candidate via the real X-02 extractor contract shape."""
    document = {
        "schema_version": "1.0",
        "experience_id": digest_document({"episode": episode_id, "s": successful}),
        "status": "CANDIDATE",
        "statement": "Episode X finished SUCCEEDED: quality 0.80.",
        "summary": "Successful task with quality 0.80.",
        "conditions": {"task_status": "SUCCEEDED", "goal_id": "goal-1"},
        "outcome": {
            "successful": successful, "task_status": "SUCCEEDED",
            "execution": {"score": 1.0}, "goal": {"score": 0.9},
            "quality": {"score": quality}, "evidence": {"score": 1.0},
        },
        "successful": successful,
        "quality_score": quality,
        "evidence_episode_ids": [episode_id],
        "evidence": {
            "evaluation_id": f"evaluation-{episode_id}",
            "correlation_id": f"correlation-{episode_id}",
            "task_status": "SUCCEEDED",
            "evidence_ids": ["evidence-1"],
            "trace_digest": "sha256:" + "0" * 64,
        },
        "extractor_version": "experience-extractor/1.0",
        "extracted_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    document["content_digest"] = digest_document({
        key: value for key, value in document.items()
        if key not in {"content_digest", "extracted_at"}
    })
    return ExperienceCandidate.from_document(document)


def agreeing_oracle() -> ReplayOracle:
    class _Oracle:
        async def replay(self, candidate: ExperienceCandidate) -> ReplayObservation:
            return ReplayObservation(
                experience_id=candidate.experience_id,
                episode_id=candidate.evidence.episode_id,
                successful=candidate.successful,
                quality_score=candidate.quality_score,
            )
    return cast(ReplayOracle, _Oracle())


def disagreeing_oracle(fail_episode_id: str) -> ReplayOracle:
    class _Oracle:
        async def replay(self, candidate: ExperienceCandidate) -> ReplayObservation:
            flipped = candidate.evidence.episode_id == fail_episode_id
            return ReplayObservation(
                experience_id=candidate.experience_id,
                episode_id=candidate.evidence.episode_id,
                successful=candidate.successful if not flipped else not candidate.successful,
                quality_score=candidate.quality_score,
            )
    return cast(ReplayOracle, _Oracle())


def drifting_oracle(deviation: float) -> ReplayOracle:
    class _Oracle:
        async def replay(self, candidate: ExperienceCandidate) -> ReplayObservation:
            # 漂移幅度恒定，方向取仍在 [0, 1] 评分界内的一侧
            drift = deviation if candidate.quality_score + deviation <= 1 else -deviation
            return ReplayObservation(
                experience_id=candidate.experience_id,
                episode_id=candidate.evidence.episode_id,
                successful=candidate.successful,
                quality_score=candidate.quality_score + drift,
            )
    return cast(ReplayOracle, _Oracle())


def group(*episode_ids: str) -> tuple[ExperienceCandidate, ...]:
    return tuple(candidate(item) for item in episode_ids)


# ---------------------------------------------------------------------------
# golden 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validates_consistent_group_with_enough_episodes() -> None:
    validator = ExperienceValidator(NOW, agreeing_oracle(), ValidationPolicy("validation/1"))
    verdict = await validator.validate(group("episode-1", "episode-2"), correlation_id="c-1")
    assert verdict.status is VerdictStatus.VALIDATED
    assert verdict.reasons == ()
    assert verdict.distinct_episodes == 2
    assert verdict.replay_agreement == 1.0
    assert verdict.policy_version == "validation/1"
    document = verdict.to_document()
    assert document["verdict_digest"] == digest_document({
        key: value for key, value in document.items()
        if key not in {"verdict_digest", "validated_at"}
    })
    assert verdict.to_document() == document


@pytest.mark.asyncio
async def test_rejects_single_episode_group_as_overfitting() -> None:
    validator = ExperienceValidator(NOW, agreeing_oracle(), ValidationPolicy("validation/1"))
    verdict = await validator.validate(group("episode-1"), correlation_id="c-2")
    assert verdict.status is VerdictStatus.REJECTED
    assert any("overfitting" in reason for reason in verdict.reasons)


@pytest.mark.asyncio
async def test_contradicted_group_is_rejected_as_whole() -> None:
    members = (candidate("episode-1", successful=True), candidate("episode-2", successful=False))
    validator = ExperienceValidator(NOW, agreeing_oracle(), ValidationPolicy("validation/1"))
    verdict = await validator.validate(members, correlation_id="c-3")
    assert verdict.status is VerdictStatus.CONTRADICTED
    assert any("contradiction" in reason for reason in verdict.reasons)


@pytest.mark.asyncio
async def test_replay_disagreement_drops_agreement_below_bound() -> None:
    validator = ExperienceValidator(
        NOW, disagreeing_oracle("episode-2"), ValidationPolicy("validation/1"),
    )
    verdict = await validator.validate(group("episode-1", "episode-2"), correlation_id="c-4")
    assert verdict.status is VerdictStatus.REJECTED
    assert any("replay" in reason for reason in verdict.reasons)
    assert verdict.replay_agreement == 0.5


@pytest.mark.asyncio
async def test_quality_drift_beyond_tolerance_counts_as_disagreement() -> None:
    validator = ExperienceValidator(
        NOW, drifting_oracle(0.30),
        ValidationPolicy("validation/1", maximum_score_deviation=0.15),
    )
    verdict = await validator.validate(group("episode-1", "episode-2"), correlation_id="c-5")
    assert verdict.status is VerdictStatus.REJECTED
    assert verdict.replay_agreement == 0.0


@pytest.mark.asyncio
async def test_policy_relaxation_allows_drift_and_keeps_validated() -> None:
    policy = ValidationPolicy("validation/2", maximum_score_deviation=0.5)
    validator = ExperienceValidator(NOW, drifting_oracle(0.30), policy)
    verdict = await validator.validate(group("episode-1", "episode-2"), correlation_id="c-6")
    assert verdict.status is VerdictStatus.VALIDATED
    assert verdict.to_document()["policy_version"] == "validation/2"


def test_policy_rejects_invalid_bounds() -> None:
    with pytest.raises(ExperienceValidationError, match="samples"):
        ValidationPolicy("validation/1", minimum_samples=0)
    with pytest.raises(ExperienceValidationError, match="agreement"):
        ValidationPolicy("validation/1", minimum_replay_agreement=1.5)
    with pytest.raises(ExperienceValidationError, match="deviation"):
        ValidationPolicy("validation/1", maximum_score_deviation=-0.1)


@pytest.mark.asyncio
async def test_validator_rejects_empty_and_mixed_claim_groups() -> None:
    validator = ExperienceValidator(NOW, agreeing_oracle(), ValidationPolicy("validation/1"))
    with pytest.raises(ExperienceValidationError, match="empty"):
        await validator.validate((), correlation_id="c-7")
    mixed = (candidate("episode-1"), candidate("episode-2"), candidate("episode-3"))
    verdict = await validator.validate(mixed, correlation_id="c-8")
    assert verdict.status is VerdictStatus.VALIDATED
    assert verdict.distinct_episodes == 3
