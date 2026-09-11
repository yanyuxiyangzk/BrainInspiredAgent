"""Run L-010 v1.5 acceptance replay and persist a machine-readable report."""
import argparse
import asyncio
import json
from pathlib import Path

from domain_sdk.factor_acceptance import (
    FactorDiscoveryReplay,
    FaultPlan,
    ReplayAcceptancePolicy,
)

FAULT_SETS = {
    "none": FaultPlan(),
    "standard": FaultPlan(
        crash_rounds=(100, 300, 500),
        review_blackout_rounds=(50, 51, 52),
        stale_pointer_rounds=(200, 400),
        facts_tamper_rounds=(250, 450),
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=581)
    parser.add_argument("--faults", choices=tuple(FAULT_SETS), default="standard")
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = ReplayAcceptancePolicy(
        total_rounds=args.rounds, seed=args.seed, faults=FAULT_SETS[args.faults]
    )
    report = asyncio.run(
        FactorDiscoveryReplay(policy).run(workdir=str(args.workdir) if args.workdir else None)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=1, sort_keys=True), encoding="utf-8")
    print(
        f"L-010 acceptance {report.status}: rounds={args.rounds} "
        f"faults={report.faults_injected} factors={report.coverage['factor_library']} "
        f"skeletons={report.diversity['distinct_skeletons']} "
        f"backtests={report.cost['backtests']}"
    )
    return 0 if report.status == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
