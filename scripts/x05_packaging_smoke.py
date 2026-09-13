"""X-05 packaging smoke: conflict resolution, TTL renewal, audited lifecycle.

黑盒驱动已安装的 bia-agent wheel：真实 SQLite（临时目录）上走完整生命周期——
矛盾候选互链 → 显式裁决消解 → VALIDATED 续期 → 过期遗忘 → 审计哈希链校验，
并注入篡改与过期数据版本两类故障验证治理边界。
"""
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from active_agent_platform.foundation import FakeClock, FakeUuidGenerator
from active_agent_platform.semantic_memory import (
    AuditEventType,
    SemanticCandidate,
    SemanticMemoryError,
    SemanticMemoryService,
    SemanticStatus,
    TtlPolicy,
)
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def candidate(*, value: object, evidence: str, data_version: str, valid_until: datetime | None = None) -> SemanticCandidate:
    return SemanticCandidate(
        claim_key="signal.reliable", claim_value=value,
        statement="The signal is reliable", summary="Reliable signal",
        evidence_episode_ids=(evidence,), scope={"universe": "smoke"},
        conditions={"phase": "review"}, confidence=0.8, data_version=data_version,
        valid_until=valid_until or NOW + timedelta(days=30), correlation_id="x05-smoke",
    )


async def seed(database: SQLiteDatabase) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    async with database.transaction() as tx:
        await tx.execute(
            "INSERT INTO plan VALUES ('plan', '{}', 'digest', 'CANDIDATE', ?, ?, ?)",
            (stamp, stamp, "x05-smoke"),
        )
        await tx.execute(
            "INSERT INTO plan_decision VALUES ('decision', 'plan', 'APPROVED', '{}', ?, ?)",
            (stamp, "x05-smoke"),
        )
        await tx.execute(
            "INSERT INTO execution_grant VALUES ('grant', 'decision', 'task', '{}', 'ACTIVE', ?, ?, ?)",
            (stamp, stamp, "x05-smoke"),
        )
        await tx.execute(
            """INSERT INTO task(task_id, grant_id, status, version, attempt, created_at,
                                 finished_at, deadline, correlation_id)
               VALUES ('task', 'grant', 'SUCCEEDED', 1, 1, ?, ?, ?, 'x05-smoke')""",
            (stamp, stamp, stamp),
        )
        for episode in ("episode-a", "episode-b", "episode-c"):
            await tx.execute(
                "INSERT INTO episode VALUES (?, 'task', '{}', ?, 'x05-smoke')",
                (episode, stamp),
            )


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="x05-smoke-"))
    database = SQLiteDatabase(workdir / "smoke.db")
    await database.initialize()
    await seed(database)
    clock = FakeClock(NOW)
    service = SemanticMemoryService(
        database, clock, FakeUuidGenerator(UUID(int=i) for i in range(1, 40)),
        ttl_policy=TtlPolicy(maximum_total_lifetime=timedelta(days=90)),
    )

    positive = await service.propose(candidate(value=True, evidence="episode-a", data_version="data/2"))
    negative = await service.propose(candidate(value=False, evidence="episode-b", data_version="data/1"))
    assert negative.contradicted_by == (positive.memory_id,), negative.contradicted_by
    blocked = await service.promote(positive.memory_id, validation_method="x03-replay")
    assert blocked.promoted is False and "contradictions" in blocked.reason

    resolution = await service.resolve_conflict(
        positive.memory_id, (negative.memory_id,), method="data_version_review"
    )
    assert resolution.rejected_loser_ids == (negative.memory_id,)
    promoted = await service.promote(positive.memory_id, validation_method="x03-replay")
    assert promoted.promoted is True, promoted.reason

    clock.advance(timedelta(days=10).total_seconds())
    renewed = await service.renew(positive.memory_id, data_version="data/2", ttl=timedelta(days=30))
    assert renewed.valid_until == NOW + timedelta(days=60), renewed.valid_until
    try:
        await service.renew(positive.memory_id, data_version="data/1", ttl=timedelta(days=30))
        raise AssertionError("stale data_version renewal must be rejected")
    except SemanticMemoryError as error:
        assert error.code == "RENEWAL_VERSION_MISMATCH", error.code

    clock.advance(timedelta(days=60).total_seconds())
    expired_ids = await service.expire_due()
    assert expired_ids == (positive.memory_id,), expired_ids
    assert await service.validated() == ()

    verification = await service.verify_audit()
    assert verification.valid and verification.broken_at is None, verification
    lifecycle = [entry.event_type for entry in await service.audit(positive.memory_id)]
    assert lifecycle == [
        AuditEventType.PROPOSED,
        AuditEventType.CONFLICT_LINKED,
        AuditEventType.PROMOTION_REFUSED,
        AuditEventType.CONFLICT_RESOLVED,
        AuditEventType.PROMOTED,
        AuditEventType.RENEWED,
        AuditEventType.EXPIRED,
    ], lifecycle

    async with database.transaction() as tx:
        await tx.execute(
            "UPDATE semantic_memory_audit SET reason = 'FORGED' WHERE event_type = 'PROMOTED'"
        )
    verification = await service.verify_audit()
    assert not verification.valid and verification.broken_at is not None, verification

    print("X-05 packaging smoke PASS:", json.dumps({
        "lifecycle_events": len(lifecycle),
        "audit_chain": "tamper-evident",
        "winner_status": promoted.record.status.value,
        "loser_status": SemanticStatus.REJECTED.value,
    }))
    return 0


raise SystemExit(asyncio.run(main()))
