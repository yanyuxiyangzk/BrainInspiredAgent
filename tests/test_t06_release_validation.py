from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.quant_agent.release_validation import real_soak, virtual_30_days


@pytest.mark.asyncio
async def test_virtual_30_days_has_no_duplicate_side_effect_or_readiness_failure(tmp_path: Path) -> None:
    output = tmp_path / "virtual.json"
    report = await virtual_30_days(tmp_path / "virtual.db", output)
    assert report.status == "PASSED"
    assert report.checkpoints == 30 and report.readiness_failures == 0
    assert report.duplicate_side_effects == 30 and report.errors == ()
    assert json.loads(output.read_text())["requested_seconds"] == 2_592_000


@pytest.mark.asyncio
async def test_real_soak_harness_checkpoints_and_validates_configuration(tmp_path: Path) -> None:
    output = tmp_path / "real.json"
    report = await real_soak(
        tmp_path / "real.db", output, duration_seconds=0.03, interval_seconds=0.005
    )
    assert report.status == "PASSED" and report.checkpoints >= 2
    persisted = json.loads(output.read_text())
    assert persisted["finished_at"] is not None and persisted["status"] == "PASSED"
    with pytest.raises(ValueError):
        await real_soak(tmp_path / "bad.db", output, duration_seconds=0)
