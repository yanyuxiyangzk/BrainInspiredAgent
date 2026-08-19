"""Domain-neutral command line entry point for the BrainAgent runtime."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import signal
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from active_agent_platform.foundation import SystemClock
from active_agent_platform.metrics import prometheus
from active_agent_platform.operations import PlatformOperations
from domain_sdk import DomainPlugin, RuntimeBuilder


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="brainagent", description="Domain-neutral BrainAgent runtime")
    root.add_argument("--database", default="brainagent.db")
    root.add_argument("--plugin", action="append", required=False, help="module:PluginClass")
    commands = root.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser("run", help="run plugin services until SIGINT/SIGTERM")
    runtime.add_argument("--run-seconds", type=float, help=argparse.SUPPRESS)
    commands.add_parser("start", help="initialize the runtime database")
    commands.add_parser("health", help="check runtime liveness and readiness")
    diagnose = commands.add_parser("diagnose", help="show a runtime diagnostic snapshot")
    diagnose.add_argument("--limit", type=int, default=20)
    metrics = commands.add_parser("metrics", help="show domain-neutral operational metrics")
    metrics.add_argument("--prometheus", action="store_true")
    trace = commands.add_parser("trace", help="query a complete correlation trace")
    trace.add_argument("correlation_id")
    commands.add_parser("migrations", help="show applied database migrations")
    commands.add_parser("status", help="show registered domain capabilities")
    return root


def _load_plugins(specs: Sequence[str] | None) -> tuple[DomainPlugin, ...]:
    plugins: list[DomainPlugin] = []
    for spec in specs or ():
        module_name, separator, class_name = spec.partition(":")
        if not separator or not module_name or not class_name:
            raise ValueError("plugin must use module:PluginClass syntax")
        candidate: Any = getattr(importlib.import_module(module_name), class_name)
        plugin = candidate() if isinstance(candidate, type) else candidate
        if not hasattr(plugin, "contribute"):
            raise TypeError(f"plugin {spec} does not implement contribute()")
        plugins.append(plugin)
    return tuple(plugins)


async def run(argv: Sequence[str]) -> int:
    args = parser().parse_args(list(argv))
    plugins = _load_plugins(args.plugin)
    clock = SystemClock()
    application = RuntimeBuilder(Path(args.database)).with_plugins(plugins).build()
    if args.command == "run":
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, application.request_shutdown)
        if args.run_seconds is not None:
            if args.run_seconds < 0:
                raise ValueError("--run-seconds must not be negative")
            loop.call_later(args.run_seconds, application.request_shutdown)
        await application.run()
        return 0
    if args.command == "start":
        await application.database.initialize()
        await application.database.close()
        print(json.dumps({"status": "READY", "database": str(Path(args.database).resolve())}))
        return 0
    if args.command == "status":
        print(json.dumps({
            "plugins": list(application.catalog.plugin_ids),
            "capabilities": len(application.catalog.capabilities),
            "skills": len(application.catalog.skills),
            "workflows": len(application.catalog.workflows),
        }, sort_keys=True))
        return 0
    if args.command in {"health", "diagnose", "metrics", "trace", "migrations"}:
        await application.database.initialize()
        try:
            operations = PlatformOperations(application.database, clock)
            if args.command == "health":
                value: object = (await operations.snapshot()).health.to_dict()
            elif args.command == "diagnose":
                value = (await operations.diagnose(recent_limit=args.limit)).to_dict()
            elif args.command == "metrics":
                snapshot = (await operations.snapshot()).metrics
                if args.prometheus:
                    print(prometheus(snapshot), end="")
                    return 0
                value = snapshot.to_dict()
            elif args.command == "migrations":
                value = {"migrations": list((await operations.snapshot()).migrations)}
            else:
                trace = await operations.trace(str(args.correlation_id))
                value = {
                    "correlation_id": trace.correlation_id,
                    **{name: [dict(item) for item in getattr(trace, name)] for name in (
                        "plans", "decisions", "grants", "tasks", "workflow_runs",
                        "node_runs", "episodes", "audits",
                    )},
                }
            print(json.dumps(value, sort_keys=True))
        finally:
            await application.database.close()
        return 0
    raise AssertionError("unreachable")


def main() -> int:
    import sys
    try:
        return asyncio.run(run(sys.argv[1:]))
    except (ImportError, TypeError, ValueError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
