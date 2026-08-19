"""Run P08 without installing packages or mutating the active environment."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from domain_sdk.release_acceptance import (
    validate_distribution_manifests,
    validate_independent_domain,
    write_report,
)


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(__file__).parents[1]
    consumer = workspace / "examples" / "research_agent"
    sys.path.insert(0, str(consumer))
    from research_agent import ExtractKeywords, ResearchAgentPlugin

    packages = validate_distribution_manifests(workspace / "distributions")
    report = await validate_independent_domain(
        args.database, ResearchAgentPlugin(), invoke=ExtractKeywords().invoke,
        virtual_days=args.virtual_days, real_seconds=args.real_seconds,
    )
    t06_status = "MISSING"
    if args.t06_report.exists():
        t06_status = str(json.loads(args.t06_report.read_text()).get("status", "INVALID"))
    decision = "RELEASABLE" if report.status == "PASSED" and t06_status == "PASSED" else "BLOCKED"
    report = replace(report, package_checks=packages, t06_status=t06_status,
                     release_decision=decision)
    write_report(args.output, report)
    return 0 if report.status == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--t06-report", type=Path, default=Path("reports/release/t06-real-24h.json"))
    parser.add_argument("--virtual-days", type=int, default=30)
    parser.add_argument("--real-seconds", type=float, default=1.0)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
