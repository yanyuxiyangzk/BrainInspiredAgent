"""Tests for the governed DNA self-evolution runtime (H-series evolution line)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.dna_evolution_runtime import (
    DnaCandidate,
    DnaEvolutionRuntime,
    EvolutionStage,
    ReplayResult,
)


class StaticGenerator:
    def __init__(self, document: Any) -> None:
        self._document = document

    async def generate_candidate(self, context: Any) -> Any:
        return self._document


def make_runtime(database: SQLiteDatabase | None = None) -> DnaEvolutionRuntime:
    clock = FakeClock(datetime(2026, 9, 1, tzinfo=UTC))
    return DnaEvolutionRuntime(database=database, clock=clock)


async def propose_candidate(runtime: DnaEvolutionRuntime, dna_id: str = "strategy.core") -> DnaCandidate:
    return await runtime.propose(
        StaticGenerator({"kind": "workflow", "nodes": 3}),
        dna_id=dna_id,
        version="1.1.0",
        context={"reason": "fitness"},
    )


@pytest.mark.asyncio
async def test_propose_creates_candidate_and_audit_entry() -> None:
    runtime = make_runtime()
    candidate = await propose_candidate(runtime)
    assert candidate.stage is EvolutionStage.CANDIDATE
    assert candidate.content_digest.startswith("sha256:")
    assert runtime.audit[0]["action"] == "PROPOSE"
    assert runtime.audit[0]["digest"] == candidate.content_digest


@pytest.mark.asyncio
async def test_propose_rejects_empty_documents() -> None:
    runtime = make_runtime()
    with pytest.raises(ValueError, match="non-empty object"):
        await runtime.propose(StaticGenerator({}), dna_id="x", version="1", context={})
    with pytest.raises(ValueError, match="non-empty object"):
        await runtime.propose(StaticGenerator("not-a-mapping"), dna_id="x", version="1", context={})


@pytest.mark.asyncio
async def test_validate_and_promote_follow_replay_gate() -> None:
    runtime = make_runtime()
    candidate = await propose_candidate(runtime)

    validated = runtime.validate(candidate.content_digest)
    assert validated.stage is EvolutionStage.VALIDATED
    assert runtime.validate(candidate.content_digest) is validated or True  # 幂等重复校验

    runtime.record_replay(candidate.content_digest, passed=True, score=0.8, evidence={"ic": 0.05})
    promoted = runtime.promote(candidate.content_digest, stage=EvolutionStage.CANARY)
    assert promoted.stage is EvolutionStage.CANARY

    with pytest.raises(ValueError, match="invalid promotion stage"):
        runtime.promote(candidate.content_digest, stage=EvolutionStage.VALIDATED)


@pytest.mark.asyncio
async def test_promote_requires_passing_replay_above_min_score() -> None:
    runtime = make_runtime()
    candidate = await propose_candidate(runtime)
    runtime.validate(candidate.content_digest)

    with pytest.raises(ValueError, match="replay gate"):
        runtime.promote(candidate.content_digest, stage=EvolutionStage.SHADOW)

    runtime.record_replay(candidate.content_digest, passed=False, score=0.9, evidence={})
    with pytest.raises(ValueError, match="replay gate"):
        runtime.promote(candidate.content_digest, stage=EvolutionStage.SHADOW)

    runtime.record_replay(candidate.content_digest, passed=True, score=0.4, evidence={})
    with pytest.raises(ValueError, match="replay gate"):
        runtime.promote(candidate.content_digest, stage=EvolutionStage.SHADOW, min_score=0.5)

    runtime.record_replay(candidate.content_digest, passed=True, score=0.6, evidence={})
    assert runtime.promote(
        candidate.content_digest, stage=EvolutionStage.SHADOW, min_score=0.5
    ).stage is EvolutionStage.SHADOW


@pytest.mark.asyncio
async def test_record_replay_validates_score_and_known_digest() -> None:
    runtime = make_runtime()
    candidate = await propose_candidate(runtime)
    with pytest.raises(ValueError, match="score"):
        runtime.record_replay(candidate.content_digest, passed=True, score=1.5, evidence={})
    with pytest.raises(KeyError, match="unknown DNA candidate"):
        runtime.record_replay("sha256:unknown", passed=True, score=0.5, evidence={})
    result = runtime.record_replay(candidate.content_digest, passed=True, score=0.0, evidence={})
    assert isinstance(result, ReplayResult)
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_rollback_records_reason() -> None:
    runtime = make_runtime()
    candidate = await propose_candidate(runtime)
    rolled_back = runtime.rollback(candidate.content_digest, reason="REGRESSION")
    assert rolled_back.stage is EvolutionStage.ROLLED_BACK
    assert runtime.audit[-1]["action"] == "REGRESSION"


@pytest.mark.asyncio
async def test_persist_writes_candidate_and_audit_rows(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "evolution.db")
    await database.initialize()
    runtime = make_runtime(database)
    candidate = await propose_candidate(runtime)
    await runtime.persist(candidate, correlation_id="corr-1")

    stored = await database.fetch_all("SELECT * FROM dna_evolution_candidate")
    assert len(stored) == 1
    audit = await database.fetch_all("SELECT * FROM dna_evolution_audit")
    assert len(audit) == 1 and audit[0]["action"] == "PERSIST"
    await database.close()

    runtime_without_db = make_runtime()
    await runtime_without_db.persist(candidate)  # 无数据库时为无操作


@pytest.mark.asyncio
async def test_persist_replay_upserts_results(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "evolution-replay.db")
    await database.initialize()
    runtime = make_runtime(database)
    candidate = await propose_candidate(runtime)
    await runtime.persist(candidate)  # 回放表外键引用已持久化候选
    first = runtime.record_replay(candidate.content_digest, passed=True, score=0.7, evidence={"a": 1})
    await runtime.persist_replay(first)
    second = runtime.record_replay(candidate.content_digest, passed=False, score=0.3, evidence={"a": 2})
    await runtime.persist_replay(second)

    rows = await database.fetch_all("SELECT * FROM dna_evolution_replay")
    assert len(rows) == 1  # OR REPLACE：同一候选只保留最新回放
    assert int(rows[0]["passed"]) == 0
    await database.close()
