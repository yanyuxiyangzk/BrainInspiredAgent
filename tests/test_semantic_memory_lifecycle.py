"""X-05: semantic-memory lifecycle governance — conflict resolution, TTL renewal, audit.

用例清单（冲突/过期注入为主）：

矛盾消解（resolve_conflict）
  T1  矛盾组可裁决：losers → REJECTED，winner 解除矛盾链接并可通过晋升
  T2  伪造消解拒绝：参与方之间不存在真实矛盾互链
  T3  终态/过期参与方拒绝：loser 已 EXPIRED、winner 已过期（valid_until <= now）
  T4  结构非法拒绝：winner 不存在、loser 含 winner 自身、loser 重复
  T5  原子性与审计：任一 loser 校验失败则整体拒绝且数据库不变；成功路径写 CONFLICT_RESOLVED

TTL 续期（renew）
  T6  VALIDATED 且 data_version 一致可续期：valid_until 单调延长并写 RENEWED 审计
  T7  data_version 不一致拒绝（以过期数据版本声明续期）
  T8  状态限制：CANDIDATE / EXPIRED 不可续期
  T9  TtlPolicy 总生命周期上限：恰好等于上限放行、超出拒绝
  T10 ttl 非正拒绝

审计（semantic_memory_audit）
  T11 全生命周期事件成可校验哈希链：PROPOSED→PROMOTED→RENEWED→EXPIRED，
      expire_due 空转不追加事件
  T12 篡改注入：改写历史审计行后 verify 定位断点；按记忆过滤查询
  T13 矛盾链接与晋升拒绝入审计：CONFLICT_LINKED 双向成对、PROMOTION_REFUSED 带原因
"""
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
    TtlPolicy,
)
from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.semantic_memory import AuditEventType
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
CORRELATION = "00000000-0000-0000-0000-000000000005"
TASK = "00000000-0000-0000-0000-000000000004"
IDS = tuple(UUID(f"00000000-0000-0000-0000-{item:012d}") for item in range(400, 480))
STAMP = NOW.isoformat().replace("+00:00", "Z")


async def make_service(tmp_path: Path, *, ttl_policy: TtlPolicy | None = None) -> tuple[SemanticMemoryService, SQLiteDatabase, FakeClock]:
    database = SQLiteDatabase(tmp_path / "lifecycle.db")
    await database.initialize()
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, ?)",
            (STAMP, STAMP, CORRELATION),
        )
        await tx.execute(
            "INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, ?)",
            (STAMP, CORRELATION),
        )
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', ?, '{}', 'ACTIVE', ?, ?, ?)",
            (TASK, STAMP, STAMP, CORRELATION),
        )
        await tx.execute(
            "INSERT INTO task(task_id, grant_id, status, version, attempt, created_at, finished_at, deadline, correlation_id)"
            " VALUES (?, 'grant', 'SUCCEEDED', 1, 1, ?, ?, ?, ?)",
            (TASK, STAMP, STAMP, STAMP, CORRELATION),
        )
        for episode_id in ("episode-1", "episode-2", "episode-3"):
            await tx.execute(
                "INSERT INTO episode VALUES (?, ?, '{}', ?, ?)",
                (episode_id, TASK, STAMP, CORRELATION),
            )
    clock = FakeClock(NOW)
    service = SemanticMemoryService(
        database, clock, FakeUuidGenerator(IDS), ttl_policy=ttl_policy
    )
    return service, database, clock


def candidate(
    *,
    value: object = True,
    evidence: tuple[str, ...] = ("episode-1",),
    valid_until: datetime | None = None,
    data_version: str = "data-v1",
) -> SemanticCandidate:
    return SemanticCandidate(
        "signal.reliable", value, "The signal is reliable", "Reliable signal",
        evidence, {"universe": "test"}, {"phase": "review"}, 0.8,
        data_version, valid_until or NOW + timedelta(days=30), CORRELATION,
    )


async def refresh(database: SQLiteDatabase, memory_id: str) -> object:
    async with database.transaction() as tx:
        return await SemanticMemoryRepository(tx).get(memory_id)


# ---------------------------------------------------------------------------
# 矛盾消解
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_conflicting_memories_can_be_resolved_in_favor_of_one_winner(tmp_path: Path) -> None:
    service, database, _ = await make_service(tmp_path)
    positive = await service.propose(candidate(value=True, evidence=("episode-1",)))
    negative = await service.propose(candidate(value=False, evidence=("episode-2",)))
    blocked = await service.promote(positive.memory_id, validation_method="review")
    assert blocked.promoted is False

    result = await service.resolve_conflict(
        positive.memory_id, (negative.memory_id,), method="data_version_review"
    )
    assert result.rejected_loser_ids == (negative.memory_id,)
    loser = await refresh(database, negative.memory_id)
    assert loser.status is SemanticStatus.REJECTED  # type: ignore[attr-defined]
    winner = await refresh(database, positive.memory_id)
    assert winner.contradicted_by == ()  # type: ignore[attr-defined]

    promoted = await service.promote(positive.memory_id, validation_method="review")
    assert promoted.promoted is True
    assert tuple(item.memory_id for item in await service.validated()) == (positive.memory_id,)


@pytest.mark.asyncio
async def test_t2_conflict_resolution_requires_real_contradiction_links(tmp_path: Path) -> None:
    service, _, _ = await make_service(tmp_path)
    first = await service.propose(candidate(value=True, evidence=("episode-1",)))
    unrelated = await service.propose(
        candidate(value=True, evidence=("episode-2",), data_version="data-v2")
    )
    with pytest.raises(SemanticMemoryError) as forged:
        await service.resolve_conflict(first.memory_id, (unrelated.memory_id,), method="review")
    assert forged.value.code == "CONFLICT_RESOLUTION_INVALID"


@pytest.mark.asyncio
async def test_t3_conflict_resolution_rejects_terminal_or_expired_participants(tmp_path: Path) -> None:
    service, _, clock = await make_service(tmp_path)
    winner = await service.propose(candidate(value=True, evidence=("episode-1",)))
    dying = await service.propose(
        candidate(value=False, evidence=("episode-2",), valid_until=NOW + timedelta(seconds=10))
    )
    clock.advance(10)
    assert await service.expire_due() == (dying.memory_id,)
    with pytest.raises(SemanticMemoryError) as terminal:
        await service.resolve_conflict(winner.memory_id, (dying.memory_id,), method="review")
    assert terminal.value.code == "CONFLICT_RESOLUTION_INVALID"

    short_winner = await service.propose(
        candidate(
            value=False, evidence=("episode-3",),
            valid_until=NOW + timedelta(seconds=15), data_version="data-v2",
        )
    )
    clock.advance(16)
    with pytest.raises(SemanticMemoryError) as expired_winner:
        await service.resolve_conflict(short_winner.memory_id, (winner.memory_id,), method="review")
    assert expired_winner.value.code == "CONFLICT_RESOLUTION_INVALID"


@pytest.mark.asyncio
async def test_t4_conflict_resolution_rejects_unknown_or_self_references(tmp_path: Path) -> None:
    service, _, _ = await make_service(tmp_path)
    winner = await service.propose(candidate(value=True, evidence=("episode-1",)))
    loser = await service.propose(candidate(value=False, evidence=("episode-2",)))
    with pytest.raises(SemanticMemoryError) as missing:
        await service.resolve_conflict(winner.memory_id, ("m-missing",), method="review")
    assert missing.value.code == "SEMANTIC_MEMORY_NOT_FOUND"
    with pytest.raises(SemanticMemoryError) as self_loop:
        await service.resolve_conflict(winner.memory_id, (winner.memory_id,), method="review")
    assert self_loop.value.code == "CONFLICT_RESOLUTION_INVALID"
    with pytest.raises(SemanticMemoryError) as duplicated:
        await service.resolve_conflict(
            winner.memory_id, (loser.memory_id, loser.memory_id), method="review"
        )
    assert duplicated.value.code == "CONFLICT_RESOLUTION_INVALID"


@pytest.mark.asyncio
async def test_t5_conflict_resolution_is_atomic_and_audited(tmp_path: Path) -> None:
    service, database, _ = await make_service(tmp_path)
    winner = await service.propose(candidate(value=True, evidence=("episode-1",)))
    linked = await service.propose(candidate(value=False, evidence=("episode-2",)))
    unlinked = await service.propose(
        candidate(value=True, evidence=("episode-3",), data_version="data-v2")
    )
    with pytest.raises(SemanticMemoryError):
        await service.resolve_conflict(
            winner.memory_id, (linked.memory_id, unlinked.memory_id), method="review"
        )
    after_failure = await refresh(database, linked.memory_id)
    assert after_failure.status is SemanticStatus.CANDIDATE  # type: ignore[attr-defined]

    result = await service.resolve_conflict(winner.memory_id, (linked.memory_id,), method="review")
    assert result.rejected_loser_ids == (linked.memory_id,)
    entries = await service.audit(winner.memory_id)
    assert entries[-1].event_type is AuditEventType.CONFLICT_RESOLVED
    loser_entries = await service.audit(linked.memory_id)
    assert loser_entries[-1].event_type is AuditEventType.CONFLICT_RESOLVED


# ---------------------------------------------------------------------------
# TTL 续期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t6_validated_memory_can_be_renewed_within_policy(tmp_path: Path) -> None:
    service, database, clock = await make_service(tmp_path)
    record = await service.propose(candidate(valid_until=NOW + timedelta(days=30)))
    assert (await service.promote(record.memory_id, validation_method="review")).promoted
    clock.advance(timedelta(days=10).total_seconds())

    renewed = await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(days=30))
    expected = NOW + timedelta(days=60)
    assert renewed.valid_until == expected
    stored = await refresh(database, record.memory_id)
    assert stored.candidate.valid_until == expected  # type: ignore[attr-defined]
    assert tuple(item.memory_id for item in await service.validated()) == (record.memory_id,)
    entries = await service.audit(record.memory_id)
    assert [entry.event_type for entry in entries][-1] is AuditEventType.RENEWED


@pytest.mark.asyncio
async def test_t7_renewal_rejects_data_version_mismatch(tmp_path: Path) -> None:
    service, _, _ = await make_service(tmp_path)
    record = await service.propose(candidate())
    await service.promote(record.memory_id, validation_method="review")
    with pytest.raises(SemanticMemoryError) as mismatch:
        await service.renew(record.memory_id, data_version="data-v2", ttl=timedelta(days=30))
    assert mismatch.value.code == "RENEWAL_VERSION_MISMATCH"


@pytest.mark.asyncio
async def test_t8_renewal_rejects_non_validated_states(tmp_path: Path) -> None:
    service, _, clock = await make_service(tmp_path)
    pending = await service.propose(candidate(evidence=("episode-1",)))
    with pytest.raises(SemanticMemoryError) as candidate_state:
        await service.renew(pending.memory_id, data_version="data-v1", ttl=timedelta(days=1))
    assert candidate_state.value.code == "RENEWAL_INVALID_STATE"

    dying = await service.propose(
        candidate(
            evidence=("episode-2",), valid_until=NOW + timedelta(seconds=10), data_version="data-v2",
        )
    )
    await service.promote(dying.memory_id, validation_method="review")
    clock.advance(10)
    assert await service.expire_due() == (dying.memory_id,)
    with pytest.raises(SemanticMemoryError) as expired_state:
        await service.renew(dying.memory_id, data_version="data-v2", ttl=timedelta(days=1))
    assert expired_state.value.code == "RENEWAL_INVALID_STATE"


@pytest.mark.asyncio
async def test_t9_renewal_enforces_maximum_total_lifetime(tmp_path: Path) -> None:
    policy = TtlPolicy(maximum_total_lifetime=timedelta(days=60))
    service, _, _ = await make_service(tmp_path, ttl_policy=policy)
    record = await service.propose(candidate(valid_until=NOW + timedelta(days=30)))
    await service.promote(record.memory_id, validation_method="review")
    with pytest.raises(SemanticMemoryError) as exceeded:
        await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(days=31))
    assert exceeded.value.code == "RENEWAL_LIFETIME_EXCEEDED"
    boundary = await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(days=30))
    assert boundary.valid_until == NOW + timedelta(days=60)


@pytest.mark.asyncio
async def test_t10_renewal_rejects_non_positive_ttl(tmp_path: Path) -> None:
    service, _, _ = await make_service(tmp_path)
    record = await service.propose(candidate())
    await service.promote(record.memory_id, validation_method="review")
    with pytest.raises(SemanticMemoryError) as zero:
        await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(0))
    assert zero.value.code == "RENEWAL_TTL_INVALID"
    with pytest.raises(SemanticMemoryError) as negative:
        await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(days=-1))
    assert negative.value.code == "RENEWAL_TTL_INVALID"


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t11_lifecycle_events_form_verifiable_hash_chain(tmp_path: Path) -> None:
    service, _, clock = await make_service(tmp_path)
    record = await service.propose(candidate(valid_until=NOW + timedelta(days=30)))
    await service.promote(record.memory_id, validation_method="review")
    await service.renew(record.memory_id, data_version="data-v1", ttl=timedelta(days=1))
    clock.advance(timedelta(days=31).total_seconds())
    assert await service.expire_due() == (record.memory_id,)
    assert await service.expire_due() == ()

    entries = await service.audit(record.memory_id)
    assert [entry.event_type for entry in entries] == [
        AuditEventType.PROPOSED,
        AuditEventType.PROMOTED,
        AuditEventType.RENEWED,
        AuditEventType.EXPIRED,
    ]
    assert [entry.sequence for entry in entries] == [1, 2, 3, 4]
    verification = await service.verify_audit()
    assert verification.valid is True and verification.broken_at is None
    assert verification.entries == 4
    assert await service.expire_due() == ()
    assert (await service.verify_audit()).entries == 4


@pytest.mark.asyncio
async def test_t12_audit_chain_detects_history_tampering(tmp_path: Path) -> None:
    service, database, _ = await make_service(tmp_path)
    record = await service.propose(candidate())
    await service.promote(record.memory_id, validation_method="review")
    assert (await service.verify_audit()).valid is True

    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE semantic_memory_audit SET event_type = 'FORGED' WHERE sequence = 2"
        )
    verification = await service.verify_audit()
    assert verification.valid is False and verification.broken_at == 2

    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE semantic_memory_audit SET event_type = 'PROMOTED' WHERE sequence = 2"
        )
    assert (await service.verify_audit()).valid is True

    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE semantic_memory_audit SET detail_json = '{\"forged\": true}' WHERE sequence = 2"
        )
    assert (await service.verify_audit()).broken_at == 2

    other = await service.propose(candidate(evidence=("episode-2",), data_version="data-v2"))
    entries = await service.audit(other.memory_id)
    assert [entry.event_type for entry in entries] == [AuditEventType.PROPOSED]
    assert (await service.verify_audit()).valid is False


@pytest.mark.asyncio
async def test_t13_conflict_linking_and_promotion_refusals_are_audited(tmp_path: Path) -> None:
    service, _, _ = await make_service(tmp_path)
    positive = await service.propose(candidate(value=True, evidence=("episode-1",)))
    negative = await service.propose(candidate(value=False, evidence=("episode-2",)))
    blocked = await service.promote(positive.memory_id, validation_method="review")
    assert blocked.promoted is False and "contradictions" in blocked.reason

    winner_entries = await service.audit(positive.memory_id)
    assert [entry.event_type for entry in winner_entries] == [
        AuditEventType.PROPOSED,
        AuditEventType.CONFLICT_LINKED,
        AuditEventType.PROMOTION_REFUSED,
    ]
    assert winner_entries[-1].reason is not None and "contradiction" in winner_entries[-1].reason
    loser_entries = await service.audit(negative.memory_id)
    assert [entry.event_type for entry in loser_entries] == [
        AuditEventType.PROPOSED,
        AuditEventType.CONFLICT_LINKED,
    ]
    all_entries = await service.audit()
    assert len(all_entries) == 5


@pytest.mark.asyncio
async def test_t14_expiry_via_promotion_and_terminal_resolution_edges(tmp_path: Path) -> None:
    with pytest.raises(SemanticMemoryError):
        TtlPolicy(policy_version="")
    with pytest.raises(SemanticMemoryError):
        TtlPolicy(maximum_total_lifetime=timedelta(0))

    service, _, clock = await make_service(tmp_path)
    expiring = await service.propose(
        candidate(evidence=("episode-1",), valid_until=NOW + timedelta(seconds=10), data_version="data-v1")
    )
    loser = await service.propose(candidate(value=False, evidence=("episode-2",), data_version="data-v2"))
    winner = await service.propose(candidate(evidence=("episode-3",), data_version="data-v3"))
    stale = await service.propose(
        candidate(value=False, evidence=("episode-1",), valid_until=NOW + timedelta(seconds=5), data_version="data-v4")
    )
    clock.advance(10)

    expired_promotion = await service.promote(expiring.memory_id, validation_method="review")
    assert expired_promotion.promoted is False and expired_promotion.reason == "candidate has expired"
    assert expired_promotion.record.status is SemanticStatus.EXPIRED
    expiring_entries = await service.audit(expiring.memory_id)
    assert [entry.event_type for entry in expiring_entries] == [
        AuditEventType.PROPOSED,
        AuditEventType.CONFLICT_LINKED,
        AuditEventType.CONFLICT_LINKED,
        AuditEventType.EXPIRED,
        AuditEventType.PROMOTION_REFUSED,
    ]
    document = expiring_entries[0].to_document()
    assert document["event_type"] == "PROPOSED"
    assert str(document["entry_digest"]).startswith("sha256:")

    resolved = await service.resolve_conflict(winner.memory_id, (loser.memory_id,), method="review")
    assert resolved.rejected_loser_ids == (loser.memory_id,)
    with pytest.raises(SemanticMemoryError) as terminal_winner:
        await service.resolve_conflict(loser.memory_id, (winner.memory_id,), method="review")
    assert terminal_winner.value.code == "CONFLICT_RESOLUTION_INVALID"
    with pytest.raises(SemanticMemoryError) as stale_loser:
        await service.resolve_conflict(winner.memory_id, (stale.memory_id,), method="review")
    assert stale_loser.value.code == "CONFLICT_RESOLUTION_INVALID"
    with pytest.raises(SemanticMemoryError) as missing_method:
        await service.resolve_conflict(winner.memory_id, (stale.memory_id,), method="")
    assert missing_method.value.code == "RESOLUTION_METHOD_REQUIRED"
    assert (await service.verify_audit()).valid is True
