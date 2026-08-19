from datetime import UTC, datetime, timedelta

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.scheduling import (
    AdmissionOutcome,
    BudgetSnapshot,
    CandidateKind,
    DeterministicCorticalPolicy,
    PolicyConfig,
    SchedulingCandidate,
    SystemMode,
    WorkClass,
)

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


def candidate(
    candidate_id: str,
    *,
    kind: CandidateKind = CandidateKind.RUN,
    work_class: WorkClass = WorkClass.BACKGROUND,
    enqueued_at: datetime = NOW,
    base_priority: float = 80,
    **overrides: object,
) -> SchedulingCandidate:
    return SchedulingCandidate(
        candidate_id=candidate_id,
        kind=kind,
        work_class=work_class,
        enqueued_at=enqueued_at,
        base_priority=base_priority,
        **overrides,  # type: ignore[arg-type]
    )


def budget(
    *,
    mode: SystemMode = SystemMode.NORMAL,
    slots: int = 1,
    cost: float = 100,
    version: str = "deterministic-1.0",
) -> BudgetSnapshot:
    return BudgetSnapshot(mode, slots, cost, version)


def test_higher_score_wins_slot_while_decisions_keep_input_order() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    low = candidate("low", base_priority=80)
    high = candidate("high", base_priority=100, work_class=WorkClass.RECOVERY)

    decisions = policy.evaluate((low, high), budget(slots=1))

    assert [decision.candidate_id for decision in decisions] == ["low", "high"]
    assert decisions[0].outcome is AdmissionOutcome.DEFER
    assert "NO_CONCURRENCY_SLOT" in decisions[0].rule_ids
    assert decisions[0].next_evaluation_at == NOW + timedelta(seconds=30)
    assert decisions[1].outcome is AdmissionOutcome.ADMIT
    assert decisions[1].next_evaluation_at is None
    assert decisions[1].budget.available_slots == 0


def test_expired_and_safe_denied_candidates_are_rejected_before_scoring() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    expired = candidate("expired", deadline=NOW)
    unsafe = candidate("unsafe", base_priority=100)

    expired_decision = policy.evaluate((expired,), budget())[0]
    safe_decision = policy.evaluate((unsafe,), budget(mode=SystemMode.SAFE))[0]

    assert expired_decision.outcome is AdmissionOutcome.REJECT
    assert expired_decision.rule_ids == ("DEADLINE_EXPIRED",)
    assert safe_decision.outcome is AdmissionOutcome.REJECT
    assert safe_decision.rule_ids == ("SAFE_MODE_DENY",)
    assert safe_decision.next_evaluation_at is None


def test_safe_allowlisted_diagnostic_can_be_admitted() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    diagnostic = candidate(
        "diagnostic",
        work_class=WorkClass.DIAGNOSTIC,
        safe_allowed=True,
        base_priority=90,
    )
    decision = policy.evaluate((diagnostic,), budget(mode=SystemMode.SAFE))[0]
    assert decision.outcome is AdmissionOutcome.ADMIT


@pytest.mark.parametrize(
    "item, expected_rule",
    [
        (candidate("dependency", dependency_ready=False), "DEPENDENCY_NOT_READY"),
        (candidate("conflict", conflict_free=False), "CONFLICT_ACTIVE"),
        (candidate("score", base_priority=10), "SCORE_BELOW_THRESHOLD"),
        (candidate("cost", estimated_cost=50, base_priority=140), "COST_BUDGET_UNAVAILABLE"),
    ],
)
def test_defer_reasons_are_explicit(
    item: SchedulingCandidate, expected_rule: str
) -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    available = budget(cost=10) if item.candidate_id == "cost" else budget()
    decision = policy.evaluate((item,), available)[0]
    assert decision.outcome is AdmissionOutcome.DEFER
    assert expected_rule in decision.rule_ids
    assert decision.reasons
    assert decision.next_evaluation_at is not None


def test_aging_eventually_admits_background_work() -> None:
    clock = FakeClock(NOW)
    policy = DeterministicCorticalPolicy(clock)
    background = candidate("background", base_priority=10)
    assert policy.evaluate((background,), budget())[0].outcome is AdmissionOutcome.DEFER

    clock.advance(7 * 60)
    aged = policy.evaluate((background,), budget())[0]
    assert aged.outcome is AdmissionOutcome.ADMIT
    assert aged.score == 80


def test_deadline_recovery_realtime_and_review_bonuses_are_deterministic() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    items = (
        candidate("deadline", base_priority=60, deadline=NOW + timedelta(seconds=150)),
        candidate("recovery", base_priority=40, work_class=WorkClass.RECOVERY),
        candidate("realtime", base_priority=65, work_class=WorkClass.REALTIME),
        candidate("background", base_priority=60),
    )
    normal = policy.evaluate(items, budget(slots=4))
    review = policy.evaluate((items[-1],), budget(mode=SystemMode.REVIEW))[0]

    assert {decision.candidate_id: decision.score for decision in normal} == {
        "deadline": 75.0,
        "recovery": 80.0,
        "realtime": 80.0,
        "background": 60.0,
    }
    assert review.score == 80.0
    assert review.outcome is AdmissionOutcome.ADMIT


def test_cost_and_penalties_are_subtracted_from_score_and_budget() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    item = candidate(
        "penalized",
        base_priority=120,
        estimated_cost=5,
        conflict_penalty=10,
        dependency_penalty=5,
    )
    decision = policy.evaluate((item,), budget(cost=20))[0]
    assert decision.score == 100
    assert decision.outcome is AdmissionOutcome.ADMIT
    assert decision.budget.remaining_cost == 15


def test_rank_helpers_select_only_requested_candidate_kind() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    stimulus = candidate("stimulus", kind=CandidateKind.STIMULUS)
    run = candidate("run", kind=CandidateKind.RUN)
    assert [item.candidate_id for item in policy.rank_stimuli((stimulus, run), budget())] == [
        "stimulus"
    ]
    assert [item.candidate_id for item in policy.rank_runs((stimulus, run), budget())] == ["run"]


def test_policy_rejects_version_mismatch_and_duplicate_candidates() -> None:
    policy = DeterministicCorticalPolicy(FakeClock(NOW))
    item = candidate("same")
    with pytest.raises(ValueError, match="policy_version"):
        policy.evaluate((item,), budget(version="other"))
    with pytest.raises(ValueError, match="unique"):
        policy.evaluate((item, item), budget(slots=2))


def test_candidate_budget_and_policy_configuration_validation() -> None:
    with pytest.raises(ValueError, match="candidate_id"):
        candidate("")
    with pytest.raises(ValueError, match="enqueued_at"):
        candidate("naive", enqueued_at=datetime.fromisoformat("2026-08-17T08:00:00"))
    with pytest.raises(ValueError, match="deadline"):
        candidate(
            "naive-deadline",
            deadline=datetime.fromisoformat("2026-08-17T08:01:00"),
        )
    with pytest.raises(ValueError, match="estimated_cost"):
        candidate("negative", estimated_cost=-1)
    with pytest.raises(ValueError, match="available_slots"):
        budget(slots=-1)
    with pytest.raises(ValueError, match="remaining_cost"):
        budget(cost=-1)
    with pytest.raises(ValueError, match="policy_version"):
        budget(version="")
    with pytest.raises(ValueError, match="version"):
        PolicyConfig(version="")
    with pytest.raises(ValueError, match="aging_interval_seconds"):
        PolicyConfig(aging_interval_seconds=0)
    with pytest.raises(ValueError, match="aging_step"):
        PolicyConfig(aging_step=-1)
