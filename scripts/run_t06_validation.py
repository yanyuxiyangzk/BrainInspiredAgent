"""Run T06 validation and continuously persist a machine-readable report."""

import argparse
import asyncio
from pathlib import Path

from apps.quant_agent.release_validation import real_soak, virtual_30_days


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("virtual", "real"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=86400)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if args.mode == "virtual":
        report = asyncio.run(virtual_30_days(args.database, args.output))
    else:
        report = asyncio.run(real_soak(
            args.database, args.output, duration_seconds=args.duration, interval_seconds=args.interval
        ))
    return 0 if report.status == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
