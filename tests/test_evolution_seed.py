"""E00 evolution seed tests: governed runs attributed to baseline DNA fitness."""

from __future__ import annotations

from pathlib import Path

import pytest

from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.evolution_seed import DEFAULT_START, seed_baseline


@pytest.mark.asyncio
async def test_seed_accumulates_fitness_for_the_active_baseline(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "seed.db")
    await database.initialize()
    try:
        report = await seed_baseline(
            database, start=DEFAULT_START, days=3, artifacts_dir=tmp_path / "art",
        )

        assert report.dna_id == "workflow.market_summary"
        assert report.version and report.content_digest.startswith("sha256:")
        assert len(report.days) == 3
        assert all(day.successful for day in report.days)
        assert report.observation_count == 3
        trade_dates = [day.trade_date for day in report.days]
        assert trade_dates == ["2026-01-05", "2026-01-06", "2026-01-07"]

        observations = await database.fetch_all(
            "SELECT * FROM dna_fitness_observation WHERE dna_id=? ORDER BY observed_at",
            (report.dna_id,),
        )
        assert len(observations) == 3
        assert all(int(row["successful"]) == 1 for row in observations)
        assert all(int(row["cost_minor"]) == 3 for row in observations)

        snapshots = await database.fetch_all(
            "SELECT * FROM dna_fitness_snapshot WHERE dna_id=? AND version=?",
            (report.dna_id, report.version),
        )
        assert len(snapshots) == 1
        assert int(snapshots[0]["sample_count"]) == 3
        assert float(snapshots[0]["success_rate"]) == 1.0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_seed_runs_are_governed_and_traceable(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "governed.db")
    await database.initialize()
    try:
        report = await seed_baseline(
            database, start=DEFAULT_START, days=2, artifacts_dir=tmp_path / "art",
        )
        for day in report.days:
            grant = await database.fetch_one(
                """SELECT g.grant_id FROM execution_grant g
                   JOIN task t ON t.grant_id = g.grant_id
                   JOIN workflow_run r ON r.task_id = t.task_id
                   WHERE r.correlation_id=?""",
                (day.correlation_id,),
            )
            assert grant is not None, "each seeded run must pass the grant-only chain"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_seed_rejects_non_positive_days(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "empty.db")
    await database.initialize()
    try:
        with pytest.raises(Exception, match="at least one day"):
            await seed_baseline(database, days=0)
    finally:
        await database.close()
