from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from active_agent_platform import (
    SemanticCandidate,
    SemanticMemoryError,
    SemanticMemoryRepository,
    SemanticMemoryService,
    SemanticStatus,
)
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
CORRELATION = "00000000-0000-0000-0000-000000000005"
TASK = "00000000-0000-0000-0000-000000000004"
IDS = tuple(UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(400, 480))


async def seed(database: SQLiteDatabase) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, ?)", (stamp, stamp, CORRELATION))
        await tx.execute("INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, ?)", (stamp, CORRELATION))
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', ?, '{}', 'ACTIVE', ?, ?, ?)",
            (TASK, stamp, stamp, CORRELATION),
        )
        await tx.execute(
            "INSERT INTO task(task_id, grant_id, status, version, attempt, created_at, finished_at, deadline, correlation_id) VALUES (?, 'grant', 'SUCCEEDED', 1, 1, ?, ?, ?, ?)",
            (TASK, stamp, stamp, stamp, CORRELATION),
        )
        for episode_id in ("episode-1", "episode-2"):
            await tx.execute(
                "INSERT INTO episode VALUES (?, ?, '{}', ?, ?)",
                (episode_id, TASK, stamp, CORRELATION),
            )


def candidate(
    *, value: object = True, evidence: tuple[str, ...] = ("episode-1",),
    valid_until: datetime = NOW + timedelta(days=30), data_version: str = "data-v1",
) -> SemanticCandidate:
    return SemanticCandidate(
        "signal.reliable", value, "The signal is reliable", "Reliable signal",
        evidence, {"universe": "test"}, {"phase": "review"}, 0.8,
        data_version, valid_until, CORRELATION,
    )


@pytest.mark.asyncio
async def test_candidate_is_evidence_backed_and_only_promoted_through_validation(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "semantic.db")
    await database.initialize()
    await seed(database)
    service = SemanticMemoryService(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    record = await service.propose(candidate())
    assert record.status is SemanticStatus.CANDIDATE
    assert record.validation_method is None and record.contradicted_by == ()
    assert await service.validated() == ()

    result = await service.promote(record.memory_id, validation_method="episode_replay/1.0")
    assert result.promoted is True
    assert result.record.status is SemanticStatus.VALIDATED
    assert result.record.validation_method == "episode_replay/1.0"
    assert tuple(item.memory_id for item in await service.validated()) == (record.memory_id,)
    repeated = await service.promote(record.memory_id, validation_method="again")
    assert repeated.promoted is False and "candidate" in repeated.reason


@pytest.mark.asyncio
async def test_conflicting_claims_are_linked_and_cannot_be_promoted(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "conflict.db")
    await database.initialize()
    await seed(database)
    service = SemanticMemoryService(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    positive = await service.propose(candidate(value=True, evidence=("episode-1",)))
    negative = await service.propose(candidate(value=False, evidence=("episode-2",)))
    assert negative.contradicted_by == (positive.memory_id,)
    async with database.transaction() as tx:
        refreshed = await SemanticMemoryRepository(tx).get(positive.memory_id)
    assert refreshed.contradicted_by == (negative.memory_id,)
    blocked = await service.promote(positive.memory_id, validation_method="review")
    assert blocked.promoted is False and "contradictions" in blocked.reason


@pytest.mark.asyncio
async def test_expiration_removes_validated_memory_from_default_retrieval(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "expiry.db")
    await database.initialize()
    await seed(database)
    clock = FakeClock(NOW)
    service = SemanticMemoryService(database, clock, FakeUuidGenerator(IDS))
    record = await service.propose(candidate(valid_until=NOW + timedelta(seconds=10)))
    assert (await service.promote(record.memory_id, validation_method="review")).promoted
    clock.advance(10)
    assert await service.expire_due() == (record.memory_id,)
    assert await service.validated() == ()
    async with database.transaction() as tx:
        expired = await SemanticMemoryRepository(tx).get(record.memory_id)
    assert expired.status is SemanticStatus.EXPIRED


@pytest.mark.asyncio
async def test_missing_evidence_expired_and_duplicate_candidates_are_rejected(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid.db")
    await database.initialize()
    await seed(database)
    service = SemanticMemoryService(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    with pytest.raises(SemanticMemoryError) as missing:
        await service.propose(candidate(evidence=("missing",)))
    assert missing.value.code == "SEMANTIC_EVIDENCE_MISSING"
    with pytest.raises(SemanticMemoryError) as expired:
        await service.propose(candidate(valid_until=NOW))
    assert expired.value.code == "SEMANTIC_CANDIDATE_EXPIRED"
    await service.propose(candidate())
    with pytest.raises(SemanticMemoryError) as duplicate:
        await service.propose(candidate(evidence=("episode-2",)))
    assert duplicate.value.code == "SEMANTIC_CANDIDATE_DUPLICATE"


@pytest.mark.asyncio
async def test_promotion_requires_method_and_existing_record(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "promotion.db")
    await database.initialize()
    await seed(database)
    service = SemanticMemoryService(database, FakeClock(NOW), FakeUuidGenerator(IDS))
    record = await service.propose(candidate())
    with pytest.raises(SemanticMemoryError) as method:
        await service.promote(record.memory_id, validation_method="")
    assert method.value.code == "VALIDATION_METHOD_REQUIRED"
    with pytest.raises(SemanticMemoryError) as missing:
        await service.promote("missing", validation_method="review")
    assert missing.value.code == "SEMANTIC_MEMORY_NOT_FOUND"


def test_candidate_contract_rejects_invalid_fields() -> None:
    with pytest.raises(ValueError, match="evidence"):
        candidate(evidence=())
    with pytest.raises(ValueError, match="evidence"):
        candidate(evidence=("same", "same"))
    with pytest.raises(ValueError, match="time"):
        candidate(valid_until=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="text"):
        SemanticCandidate(
            "", True, "statement", "summary", ("episode-1",), {}, {}, 0.5,
            "v1", NOW + timedelta(days=1), CORRELATION,
        )
    with pytest.raises(ValueError, match="confidence"):
        SemanticCandidate(
            "claim", True, "statement", "summary", ("episode-1",), {}, {}, 1.1,
            "v1", NOW + timedelta(days=1), CORRELATION,
        )
