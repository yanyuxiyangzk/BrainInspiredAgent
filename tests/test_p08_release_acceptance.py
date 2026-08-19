from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from domain_sdk.contracts import JsonValue
from domain_sdk.release_acceptance import (
    validate_distribution_manifests,
    validate_independent_domain,
    write_report,
)


def _research() -> tuple[object, object]:
    root = Path(__file__).parents[1] / "examples" / "research_agent"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research_agent import ExtractKeywords, ResearchAgentPlugin
    return ResearchAgentPlugin(), ExtractKeywords()


def test_distribution_boundaries_are_validated_without_installing() -> None:
    root = Path(__file__).parents[1] / "distributions"
    checks = validate_distribution_manifests(root)
    assert checks == (
        "brainagent-kernel:BOUNDARY_OK",
        "brainagent-platform:BOUNDARY_OK",
        "brainagent-domain-sdk:BOUNDARY_OK",
    )


@pytest.mark.asyncio
async def test_independent_research_virtual_and_real_release_validation(tmp_path: Path) -> None:
    plugin, skill = _research()
    report = await validate_independent_domain(
        tmp_path / "research.db", plugin, invoke=skill.invoke,  # type: ignore[attr-defined,arg-type]
        virtual_days=30, real_seconds=0.03,
    )
    assert report.status == "PASSED"
    assert report.virtual_checkpoints == 30
    assert report.real_checkpoints >= 1
    assert report.readiness_failures == 0
    assert report.deterministic_replays == 30
    assert report.release_decision == "BLOCKED"
    output = tmp_path / "report.json"
    write_report(output, replace(report, t06_status="PASSED", release_decision="RELEASABLE"))
    assert '"release_decision": "RELEASABLE"' in output.read_text()


@pytest.mark.asyncio
async def test_invalid_release_duration_is_rejected(tmp_path: Path) -> None:
    plugin, skill = _research()
    with pytest.raises(ValueError, match="durations"):
        await validate_independent_domain(
            tmp_path / "invalid.db", plugin, invoke=skill.invoke,  # type: ignore[attr-defined,arg-type]
            virtual_days=0,
        )


@pytest.mark.asyncio
async def test_nondeterministic_pure_skill_fails_release_gate(tmp_path: Path) -> None:
    plugin, _ = _research()
    calls = 0

    async def unstable(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        nonlocal calls
        del value
        calls += 1
        return {"call": calls}

    report = await validate_independent_domain(
        tmp_path / "unstable.db", plugin, invoke=unstable,  # type: ignore[arg-type]
        virtual_days=1, real_seconds=0.01,
    )
    assert report.status == "FAILED"
    assert "PURE skill output changed" in report.errors[0]
