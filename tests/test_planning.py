from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.planning import (
    CandidatePlan,
    GrantIssuer,
    GrantStatus,
    PlanDecision,
    PlanningError,
    PlanningRepository,
)
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, 3, 0, tzinfo=UTC)


def plan_document(*, plan_id: str = "plan-1") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "plan_id": plan_id,
        "status": "CANDIDATE",
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "correlation_id": "corr-1",
        "trigger": {"type": "GOAL", "source_id": "g", "occurred_at": NOW.isoformat()},
        "goal": {"goal_id": "research", "priority": 80},
        "reason": "test",
        "evidence": [],
        "tasks": [{"task_id": "task-1"}],
        "requested_budget": {"max_tokens": 0},
        "policy_context": {"brain_mode": "NORMAL"},
    }


def decision_document(
    *, decision_id: str = "decision-1", decision: str = "APPROVED", decided_at: datetime = NOW
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "decision_id": decision_id,
        "plan_id": "plan-1",
        "decision": decision,
        "decided_at": decided_at.isoformat(),
        "validator_version": "1",
        "policy_version": "1",
        "world_snapshot_id": "world-1",
        "reasons": ["allowed"],
        "correlation_id": "corr-1",
    }


def grant_document(*, grant_id: str = "grant-1", task_id: str = "task-1") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "grant_id": grant_id,
        "decision_id": "decision-1",
        "plan_id": "plan-1",
        "task_id": task_id,
        "workflow": {"workflow_id": "summary", "version": "1.0.0", "digest": "sha256:x"},
        "bindings": [{"node_id": "read", "skill_id": "fake"}],
        "policy_version": "1",
        "world_snapshot_id": "world-1",
        "memory_snapshot_id": "memory-1",
        "allowed_permissions": [],
        "budget": {"max_duration_seconds": 60},
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "consumption": "SINGLE_TASK_MULTI_ATTEMPT",
        "correlation_id": "corr-1",
    }


async def database(path: Path) -> SQLiteDatabase:
    result = SQLiteDatabase(path)
    await result.initialize()
    return result


@pytest.mark.asyncio
async def test_candidate_plan_is_immutable_digest_checked_and_unique(tmp_path: Path) -> None:
    store = await database(tmp_path / "facts.db")
    document = plan_document()
    plan = CandidatePlan.create(document)
    document["reason"] = "mutated outside"
    assert plan.document["reason"] == "test"
    with pytest.raises(TypeError):
        plan.document["reason"] = "no"  # type: ignore[index]
    async with store.transaction() as transaction:
        repository = PlanningRepository(transaction)
        await repository.add_plan(plan)
        restored = await repository.get_plan("plan-1")
        assert restored == plan
        with pytest.raises(PlanningError) as error:
            await repository.add_plan(CandidatePlan.create(plan_document()))
        assert error.value.code == "PLAN_ALREADY_EXISTS"
    async with store.transaction() as transaction:
        await transaction.execute("UPDATE plan SET digest = 'bad' WHERE plan_id = 'plan-1'")
        with pytest.raises(PlanningError) as error:
            await PlanningRepository(transaction).get_plan("plan-1")
        assert error.value.code == "PLAN_DIGEST_MISMATCH"
    await store.close()


@pytest.mark.asyncio
async def test_plan_has_one_final_decision_and_expired_plan_cannot_be_approved(
    tmp_path: Path,
) -> None:
    store = await database(tmp_path / "facts.db")
    async with store.transaction() as transaction:
        repository = PlanningRepository(transaction)
        await repository.add_plan(CandidatePlan.create(plan_document()))
        decision = PlanDecision.create(decision_document())
        await repository.add_decision(decision)
        assert await repository.get_decision("decision-1") == decision
        with pytest.raises(PlanningError) as error:
            await repository.add_decision(
                PlanDecision.create(decision_document(decision_id="decision-2"))
            )
        assert error.value.code == "PLAN_ALREADY_DECIDED"
    await store.close()

    expired = await database(tmp_path / "expired.db")
    async with expired.transaction() as transaction:
        repository = PlanningRepository(transaction)
        await repository.add_plan(CandidatePlan.create(plan_document()))
        with pytest.raises(PlanningError) as error:
            await repository.add_decision(
                PlanDecision.create(decision_document(decided_at=NOW + timedelta(minutes=10)))
            )
        assert error.value.code == "PLAN_EXPIRED"
    await expired.close()


@pytest.mark.asyncio
async def test_grant_issue_multi_attempt_revoke_and_expire(tmp_path: Path) -> None:
    store = await database(tmp_path / "facts.db")
    async with store.transaction() as transaction:
        plans = PlanningRepository(transaction)
        await plans.add_plan(CandidatePlan.create(plan_document()))
        await plans.add_decision(PlanDecision.create(decision_document()))
        issuer = GrantIssuer(transaction)
        grant = await issuer.issue(grant_document())
        assert grant.status is GrantStatus.ACTIVE
        with pytest.raises(TypeError):
            grant.document["task_id"] = "other"  # type: ignore[index]
        first = await issuer.authorize_attempt("grant-1", "task-1", 1, authorized_at=NOW)
        second = await issuer.authorize_attempt(
            "grant-1", "task-1", 2, authorized_at=NOW + timedelta(seconds=1)
        )
        assert (first.attempt, second.attempt) == (1, 2)
        with pytest.raises(PlanningError, match="sequential"):
            await issuer.authorize_attempt("grant-1", "task-1", 4, authorized_at=NOW)
        with pytest.raises(PlanningError) as error:
            await issuer.authorize_attempt("grant-1", "other", 3, authorized_at=NOW)
        assert error.value.code == "GRANT_TASK_MISMATCH"
        revoked = await issuer.revoke(
            "grant-1", reason="safe mode", occurred_at=NOW + timedelta(seconds=2)
        )
        assert revoked.status is GrantStatus.REVOKED
        with pytest.raises(PlanningError) as error:
            await issuer.authorize_attempt("grant-1", "task-1", 3, authorized_at=NOW)
        assert error.value.code == "GRANT_NOT_ACTIVE"
    await store.close()

    expiry_store = await database(tmp_path / "expiry.db")
    async with expiry_store.transaction() as transaction:
        plans = PlanningRepository(transaction)
        await plans.add_plan(CandidatePlan.create(plan_document()))
        await plans.add_decision(PlanDecision.create(decision_document()))
        issuer = GrantIssuer(transaction)
        await issuer.issue(grant_document())
        with pytest.raises(PlanningError, match="not arrived"):
            await issuer.expire("grant-1", occurred_at=NOW)
        expired = await issuer.expire(
            "grant-1", occurred_at=NOW + timedelta(minutes=5)
        )
        assert expired.status is GrantStatus.EXPIRED
    await expiry_store.close()


@pytest.mark.asyncio
async def test_rejected_decision_and_invalid_grant_are_never_authorized(tmp_path: Path) -> None:
    store = await database(tmp_path / "facts.db")
    async with store.transaction() as transaction:
        plans = PlanningRepository(transaction)
        await plans.add_plan(CandidatePlan.create(plan_document()))
        await plans.add_decision(
            PlanDecision.create(decision_document(decision="REJECTED"))
        )
        with pytest.raises(PlanningError) as error:
            await GrantIssuer(transaction).issue(grant_document())
        assert error.value.code == "GRANT_NOT_ALLOWED"
        count = await transaction.fetch_one("SELECT count(*) FROM execution_grant")
        assert count is not None and count[0] == 0
    await store.close()


def test_plan_and_decision_models_reject_invalid_documents() -> None:
    invalid = plan_document()
    invalid["status"] = "APPROVED"
    with pytest.raises(PlanningError, match="CANDIDATE"):
        CandidatePlan.create(invalid)
    invalid = plan_document()
    invalid["expires_at"] = NOW.isoformat()
    with pytest.raises(PlanningError, match="expiry"):
        CandidatePlan.create(invalid)
    invalid = plan_document()
    invalid["created_at"] = "not-a-date"
    with pytest.raises(PlanningError, match="ISO"):
        CandidatePlan.create(invalid)
    invalid = decision_document(decision="MAYBE")
    with pytest.raises(PlanningError, match="unsupported"):
        PlanDecision.create(invalid)


@pytest.mark.asyncio
async def test_repository_and_grant_negative_paths_are_explicit(tmp_path: Path) -> None:
    store = await database(tmp_path / "facts.db")
    async with store.transaction() as transaction:
        plans = PlanningRepository(transaction)
        with pytest.raises(PlanningError) as error:
            await plans.get_plan("missing")
        assert error.value.code == "PLAN_NOT_FOUND"
        with pytest.raises(PlanningError) as error:
            await plans.get_decision("missing")
        assert error.value.code == "PLAN_DECISION_NOT_FOUND"
        await plans.add_plan(CandidatePlan.create(plan_document()))
        wrong_correlation = decision_document()
        wrong_correlation["correlation_id"] = "other"
        with pytest.raises(PlanningError, match="correlation"):
            await plans.add_decision(PlanDecision.create(wrong_correlation))
        await plans.add_decision(PlanDecision.create(decision_document()))
        issuer = GrantIssuer(transaction)
        invalid = grant_document()
        invalid["plan_id"] = "other"
        with pytest.raises(PlanningError, match="does not match"):
            await issuer.issue(invalid)
        invalid = grant_document()
        invalid["expires_at"] = (NOW + timedelta(minutes=11)).isoformat()
        with pytest.raises(PlanningError, match="validity"):
            await issuer.issue(invalid)
        invalid = grant_document()
        invalid["consumption"] = "ONCE"
        with pytest.raises(PlanningError, match="consumption"):
            await issuer.issue(invalid)
        invalid = grant_document()
        invalid["correlation_id"] = "other"
        with pytest.raises(PlanningError, match="correlation"):
            await issuer.issue(invalid)
        await issuer.issue(grant_document())
        with pytest.raises(PlanningError) as error:
            await issuer.issue(grant_document(grant_id="grant-2"))
        assert error.value.code == "GRANT_ALREADY_EXISTS"
        with pytest.raises(PlanningError) as error:
            await issuer.authorize_attempt(
                "grant-1", "task-1", 1, authorized_at=NOW + timedelta(minutes=5)
            )
        assert error.value.code == "GRANT_EXPIRED"
        with pytest.raises(PlanningError, match="reason"):
            await issuer.revoke("grant-1", reason="", occurred_at=NOW)
        await issuer.revoke("grant-1", reason="stop", occurred_at=NOW)
        with pytest.raises(PlanningError) as error:
            await issuer.revoke("grant-1", reason="again", occurred_at=NOW)
        assert error.value.code == "GRANT_NOT_ACTIVE"
    await store.close()
