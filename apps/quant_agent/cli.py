"""Local JSON/Markdown CLI for platform status and quant insight delivery."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, time
from pathlib import Path
from typing import Any, TextIO, cast

from active_agent_platform.diagnostics import HealthService
from active_agent_platform.foundation import SystemClock, Uuid7Generator
from active_agent_platform.metrics import PlatformMetrics, prometheus
from active_agent_platform.sensory import CommandAdapter, CommandRejected
from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.trace import TraceQuery
from apps.quant_agent.command_sink import SQLiteEventSink
from apps.quant_agent.delivery import InsightDeliveryService
from apps.quant_agent.insights import InsightExplanation, MarketInsightQuery

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 4
EXIT_UNAVAILABLE = 5


class _LegacyEngineAdapter:  # pragma: no cover - compatibility for pre-U03 test plugins
    """Compatibility adapter for tests/plugins returning pre-U03 components."""
    def __init__(self, service: object) -> None:
        self._service = service
        self._started = asyncio.Event()

    async def run(self) -> None:
        self._started.set()
        await self._service.start()  # type: ignore[attr-defined]
        try:
            await self._service.serve()  # type: ignore[attr-defined]
        finally:
            await self._service.quiesce()  # type: ignore[attr-defined]
            await self._service.checkpoint()  # type: ignore[attr-defined]
            await self._service.stop()  # type: ignore[attr-defined]

    async def wait_started(self) -> None:
        await self._started.wait()

    def request_shutdown(self) -> None:
        return

    def health(self) -> object:
        class Snapshot:
            instance_id = "legacy"
            system = type("System", (), {"value": "HEALTHY"})()
        return Snapshot()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bia", description="Local brain-agent control and query CLI")
    root.add_argument(
        "--database", default=str(Path.home() / ".local" / "state" / "bia" / "bia.db"),
        help="SQLite fact database",
    )
    root.add_argument("--format", choices=("json", "markdown"), default="json")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("shell", help="open the interactive BIA slash-command terminal")
    commands.add_parser("start", help="initialize the local fact store")
    runtime = commands.add_parser("run", help="run the quant command worker until SIGINT/SIGTERM")
    runtime.add_argument("--daily-review-at", default="18:00", metavar="HH:MM")
    runtime.add_argument("--daily-review-timezone", default="Asia/Shanghai")
    runtime.add_argument("--daily-review-window-seconds", type=float, default=60.0)
    runtime.add_argument("--daily-review-max-missed-seconds", type=float, default=86400.0)
    runtime.add_argument("--daily-review-missed-policy", choices=("SKIP", "FIRE_ONCE"),
                         default="FIRE_ONCE")
    runtime.add_argument("--daily-review-all-days", action="store_true")
    commands.add_parser("status", help="show durable platform status")
    for name, choices in {
        "system": ("status", "health", "diagnose", "metrics", "logs", "migrations"),
        "brain": ("state", "areas", "cycles"),
        "events": ("recent", "show", "correlation", "inbox", "outbox", "dead-letter"),
        "plans": ("recent", "show", "rejected"),
        "tasks": ("list", "running", "failed", "show", "trace", "cancel", "retry"),
        "catalog": ("capabilities", "skills", "workflows"),
        "skills": ("list", "show", "health", "bindings"),
        "workflows": ("list", "active", "show", "runs"),
        "dna": ("list", "active", "show", "lineage", "explain", "executions", "transition"),
        "evolution": ("candidates", "fitness", "datasets", "replay", "compare", "campaigns"),
    }.items():
        query = commands.add_parser(name, help=f"query {name}")
        query.add_argument("view", choices=choices, nargs="?", default=choices[0])
        query.add_argument("identifier", nargs="?")
        query.add_argument("--limit", type=int, default=20)
        if name == "dna":
            query.add_argument("--version")
            query.add_argument("--to", choices=("VALIDATED", "SHADOW", "CANARY", "ACTIVE", "DEPRECATED", "RETIRED"))
            query.add_argument("--revision", type=int)
            query.add_argument("--reason")
            query.add_argument("--yes", action="store_true")
    subscriptions_query = commands.add_parser("subscriptions", help="notification preferences")
    subscription_commands = subscriptions_query.add_subparsers(dest="subscription_command", required=True)
    subscribe = subscription_commands.add_parser("add")
    subscribe.add_argument("subscription_id")
    subscribe.add_argument("--topic", default="market_summary")
    subscribe.add_argument("--minimum-level", default="INFO", choices=("INFO", "WARNING", "ERROR"))
    subscribe.add_argument("--channel", default="local")
    subscribe.add_argument("--hourly-limit", type=int, default=10)
    subscribe.add_argument("--quiet-start-hour", type=int)
    subscribe.add_argument("--quiet-end-hour", type=int)
    subscription_commands.add_parser("list").add_argument("subscription_id", nargs="?")
    for action in ("enable", "disable"):
        subscription_commands.add_parser(action).add_argument("subscription_id")
    loop_status = commands.add_parser("loop", help="inspect LoopEngine state")
    loop_status.add_argument("loop_command", choices=("status", "services", "lag", "checkpoints"),
                             nargs="?", default="status")
    commands.add_parser("health", help="check database liveness and readiness")
    diagnose = commands.add_parser("diagnose", help="show a read-only diagnostic snapshot")
    diagnose.add_argument("--limit", type=int, default=20)
    metrics = commands.add_parser("metrics", help="show operational metrics")
    metrics.add_argument("--prometheus", action="store_true")
    commands.add_parser("stop", help="request shutdown (foreground runtime only)")
    inject = commands.add_parser("inject", help="inject an allowlisted command through CommandAdapter")
    inject.add_argument("injected_command")
    inject.add_argument("--args", default="{}", help="JSON object of command arguments")
    inject.add_argument("--idempotency-key")
    market = commands.add_parser("market", help="market application commands")
    market_commands = market.add_subparsers(dest="market_command", required=True)
    summary = market_commands.add_parser("summary")
    summary.add_argument("--symbols", default="INDEX.TEST")
    summary.add_argument("--trade-date")
    summary.add_argument("--title", default="Market summary")
    read = subscription_commands.add_parser("read")
    read.add_argument("delivery_id")
    delivery = subscription_commands.add_parser("deliver")
    delivery.add_argument("subscription_id")
    delivery.add_argument("insight_id")
    delivery.add_argument("--level", default="INFO", choices=("INFO", "WARNING", "ERROR"))
    replay = commands.add_parser("replay", help="show a correlation trace")
    replay.add_argument("correlation_id")
    logs = commands.add_parser("log", help="show recent audit records")
    logs.add_argument("--limit", type=int, default=20)
    command_status = commands.add_parser("commands", help="query durable command status")
    command_status.add_argument("command_id", nargs="?")
    command_status.add_argument("--limit", type=int, default=20)
    insights = commands.add_parser("insights", help="query MarketInsight projections")
    insight_commands = insights.add_subparsers(dest="insight_command", required=True)
    latest = insight_commands.add_parser("latest")
    latest.add_argument("--limit", type=int, default=10)
    latest.add_argument("--cursor")
    latest.add_argument("--stale", choices=("include", "exclude", "only"), default="include")
    latest.add_argument("--symbol")
    latest.add_argument("--since", type=datetime.fromisoformat)
    latest.add_argument("--until", type=datetime.fromisoformat)
    latest.add_argument("--type", dest="insight_type", choices=("market_summary",))
    show = insight_commands.add_parser("show")
    show.add_argument("insight_id")
    explain = insight_commands.add_parser("explain")
    explain.add_argument("insight_id")
    return root


async def run(
    argv: Sequence[str], stdout: TextIO, stderr: TextIO, stdin: TextIO | None = None,
) -> int:
    try:
        args = parser().parse_args(list(argv))
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else EXIT_USAGE
    database_path = Path(args.database)
    if args.command == "shell":
        import sys

        from apps.quant_agent.shell import interactive
        return await interactive(database_path, stdin or sys.stdin, stdout, stderr)
    if args.command in {"start", "run"}:
        from apps.quant_agent.startup import prepare_runtime_paths
        try:
            prepare_runtime_paths(database_path)
        except OSError as error:
            stderr.write(_error("STARTUP_PATH_INVALID", str(error)) + "\n")
            return EXIT_UNAVAILABLE
    if args.command == "run":
        return await _run_runtime(database_path, stdout, stderr, args)
    database = SQLiteDatabase(database_path)
    try:
        await database.initialize()
        value = await _dispatch(database, args)
        if isinstance(value, _Prometheus):
            stdout.write(value.value)
        else:
            stdout.write(_render(value, args.format) + "\n")
        return EXIT_OK
    except LookupError as error:
        stderr.write(_error("NOT_FOUND", str(error)) + "\n")
        return EXIT_NOT_FOUND
    except (OSError, RuntimeError) as error:
        stderr.write(_error("UNAVAILABLE", str(error)) + "\n")
        return EXIT_UNAVAILABLE
    except CommandRejected as error:
        stderr.write(_error(error.code, str(error)) + "\n")
        return EXIT_USAGE
    except ValueError as error:
        stderr.write(_error("INVALID_ARGUMENT", str(error)) + "\n")
        return EXIT_USAGE
    finally:
        await database.close()


async def _dispatch(database: SQLiteDatabase, args: argparse.Namespace) -> object:
    if args.command == "start":
        return {"status": "READY", "database": str(Path(args.database).resolve())}
    if args.command in {"system", "brain", "events", "plans", "tasks", "catalog",
                        "skills", "workflows", "dna", "evolution"}:
        if args.command == "tasks" and args.view in {"cancel", "retry"}:
            if not args.identifier:
                raise ValueError("task identifier is required")
            command = f"task.{args.view}"
            adapter = CommandAdapter(
                "bia.cli", SystemClock(), Uuid7Generator(SystemClock()),
                SQLiteEventSink(database, SystemClock()), allowed_commands={command: False},
            )
            result = await adapter.inject(
                command, {"task_id": args.identifier},
                idempotency_key=f"{command}:{args.identifier}",
            )
            return {"status": result.outcome, "message_id": result.msg_id,
                    "command": command, "governed": True}
        from apps.quant_agent.query_surface import CommandSurfaceQuery
        surface_query = CommandSurfaceQuery(database)
        if not 1 <= args.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if args.command == "system":
            if args.view == "health":
                return (await HealthService(database, SystemClock()).check()).to_dict()
            if args.view == "diagnose":
                return (await HealthService(database, SystemClock()).diagnose(
                    recent_limit=args.limit
                )).to_dict()
            if args.view == "metrics":
                return (await PlatformMetrics(database, SystemClock()).snapshot()).to_dict()
            if args.view == "logs":
                rows = await database.fetch_all(
                    "SELECT * FROM audit_record ORDER BY occurred_at DESC LIMIT ?", (args.limit,)
                )
                return {"audits": [dict(row) for row in rows]}
            if args.view == "migrations":
                rows = await database.fetch_all(
                    "SELECT version,checksum FROM schema_migration ORDER BY version"
                )
                return {"migrations": [dict(row) for row in rows]}
            counts = {}
            for table in ("task", "workflow_run", "episode", "outbox_event"):
                row = await database.fetch_one(f"SELECT count(*) AS total FROM {table}")
                counts[table] = 0 if row is None else int(row["total"])
            return {"status": "READY", "facts": counts}
        if args.command == "brain":
            return await surface_query.brain(args.view, args.limit)
        if args.command == "events":
            return await surface_query.events(args.view, args.limit, args.identifier)
        if args.command == "plans":
            return await surface_query.plans(args.view, args.limit, args.identifier)
        if args.command == "tasks":
            return await surface_query.tasks(args.view, args.limit, args.identifier)
        if args.command == "skills":
            return await surface_query.catalog("skills", args.limit, args.identifier)
        if args.command == "workflows":
            return await surface_query.catalog("workflows", args.limit, args.identifier)
        if args.command == "dna":
            if args.view == "transition":
                if not args.identifier or not args.version or not args.to or args.revision is None or not args.reason:
                    raise ValueError("DNA transition requires ID, --version, --to, --revision and --reason")
                if not args.yes:
                    raise ValueError("DNA transition requires --yes confirmation")
                from domain_sdk.dna import DnaStatus
                from domain_sdk.dna_repository import PersistentDnaRegistry
                registry = PersistentDnaRegistry(database, SystemClock(), Uuid7Generator(SystemClock()))
                record = await registry.transition(
                    args.identifier, args.version, DnaStatus(args.to),
                    expected_revision=args.revision, reason=args.reason,
                    correlation_id=f"cli:dna:{args.identifier}:{args.version}",
                )
                return {"status": record.dna.status.value, "dna_id": record.dna.dna_id,
                        "version": record.dna.version, "revision": record.revision,
                        "governed": True}
            return await surface_query.dna(args.view, args.limit, args.identifier)
        if args.command == "evolution":
            return await surface_query.evolution(args.view, args.limit, args.identifier)
        return await surface_query.catalog(args.view, args.limit, args.identifier)
    if args.command == "stop":
        return {"status": "STOP_REQUEST_ACCEPTED", "note": "no background daemon is managed"}
    if args.command == "inject" or args.command == "market":
        if args.command == "market":
            command, raw_args = "market.summary", {
                "symbols": [item for item in args.symbols.split(",") if item],
                "trade_date": args.trade_date, "title": args.title,
            }
            key = f"market.summary:{args.trade_date or 'today'}:{','.join(raw_args['symbols'])}"
        else:
            try:
                raw_args = json.loads(args.args)
            except json.JSONDecodeError as error:
                raise ValueError("--args must be a JSON object") from error
            if not isinstance(raw_args, Mapping):
                raise ValueError("--args must be a JSON object")
            command, key = args.injected_command, args.idempotency_key
        adapter = CommandAdapter(
            "bia.cli", SystemClock(), Uuid7Generator(SystemClock()), SQLiteEventSink(database, SystemClock()),
            allowed_commands={"status": False, "market.summary": False},
        )
        result = await adapter.inject(command, raw_args, idempotency_key=key)
        return {"status": result.outcome, "message_id": result.msg_id,
                "command": command, "governed": True}
    if args.command == "subscriptions":
        service = InsightDeliveryService(database, SystemClock())
        if args.subscription_command == "add":
            await service.subscribe(args.subscription_id, topic=args.topic,
                                    minimum_level=args.minimum_level, channel=args.channel,
                                    hourly_limit=args.hourly_limit,
                                    quiet_start_hour=args.quiet_start_hour,
                                    quiet_end_hour=args.quiet_end_hour)
            return {"status": "SUBSCRIBED", "subscription_id": args.subscription_id}
        if args.subscription_command in {"enable", "disable"}:
            changed = await service.set_enabled(
                args.subscription_id, enabled=args.subscription_command == "enable"
            )
            return {"status": "ENABLED" if args.subscription_command == "enable" else "DISABLED",
                    "subscription_id": args.subscription_id, "changed": changed}
        if args.subscription_command == "deliver":
            delivered = await service.deliver(
                args.subscription_id, args.insight_id, level=args.level
            )
            return {"status": delivered.status, "delivery_id": delivered.delivery_id}
        if args.subscription_command == "list":
            if args.subscription_id:
                return {"deliveries": list(await service.deliveries(args.subscription_id))}
            from apps.quant_agent.query_surface import CommandSurfaceQuery
            return await CommandSurfaceQuery(database).subscriptions(None, 100)
        return {"status": "READ" if await service.mark_read(args.delivery_id) else "NOT_FOUND",
                "delivery_id": args.delivery_id}
    if args.command == "metrics":
        snapshot = await PlatformMetrics(database, SystemClock()).snapshot()
        return _Prometheus(prometheus(snapshot)) if args.prometheus else snapshot.to_dict()
    if args.command == "health":
        return (await HealthService(database, SystemClock()).check()).to_dict()
    if args.command == "diagnose":
        return (await HealthService(database, SystemClock()).diagnose(
            recent_limit=args.limit
        )).to_dict()
    if args.command == "status":
        counts = {}
        for table in ("task", "workflow_run", "episode", "local_notification_delivery"):
            row = await database.fetch_one(f"SELECT count(*) AS count FROM {table}")
            counts[table] = 0 if row is None else int(row["count"])
        return {"status": "HEALTHY", "ready": True, "facts": counts}
    if args.command == "loop":
        return {
            "status": "UNKNOWN",
            "scope": args.loop_command,
            "reason": "LoopEngine snapshots are process-local",
            "next_action": "run bia and use /loop inside the interactive terminal",
        }
    if args.command == "replay":
        bundle = await TraceQuery(database).by_correlation(str(args.correlation_id))
        return {name: _plain(getattr(bundle, name)) for name in (
            "plans", "decisions", "grants", "tasks", "workflow_runs", "node_runs",
            "episodes", "audits",
        )} | {"correlation_id": bundle.correlation_id}
    if args.command == "log":
        if not 1 <= args.limit <= 1000:
            raise ValueError("log limit must be between 1 and 1000")
        rows = await database.fetch_all(
            "SELECT * FROM audit_record ORDER BY occurred_at DESC LIMIT ?", (args.limit,)
        )
        return {"audits": [dict(row) for row in rows]}
    if args.command == "commands":
        if not 1 <= args.limit <= 1000:
            raise ValueError("command limit must be between 1 and 1000")
        if args.command_id:
            row = await database.fetch_one(
                "SELECT * FROM command_execution WHERE command_id=?", (args.command_id,)
            )
            if row is None:
                raise LookupError(f"command not found: {args.command_id}")
            return _command_row(row)
        rows = await database.fetch_all(
            "SELECT * FROM command_execution ORDER BY accepted_at DESC LIMIT ?", (args.limit,)
        )
        return {"commands": [_command_row(row) for row in rows]}
    query = MarketInsightQuery(database)
    if args.insight_command == "latest":
        values = await query.latest(limit=args.limit, cursor=args.cursor, stale=args.stale,
                                    symbol=args.symbol, since=args.since, until=args.until,
                                    insight_type=args.insight_type)
        return {"insights": [item.to_dict() for item in values],
                "next_cursor": values[-1].insight_id if len(values) == args.limit else None}
    if args.insight_command == "show":
        return (await query.show(args.insight_id)).to_dict()
    explanation = await query.explain(args.insight_id)
    return _explanation(explanation)


def _render(value: object, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, Mapping) and "insight_id" in value:
        return _insight_markdown(value)
    if isinstance(value, Mapping) and isinstance(value.get("insights"), list):
        items = value["insights"]
        return "\n\n".join(_insight_markdown(item) for item in items if isinstance(item, Mapping)) \
            or "_No insights._"
    return "```json\n" + json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n```"


def _insight_markdown(value: Mapping[object, object]) -> str:
    evidence = value.get("evidence", [])
    lines = [f"# {value.get('title', 'Market insight')}", "", str(value.get("summary", "")), "",
             f"- Insight: `{value.get('insight_id')}`",
             f"- Fresh until: `{value.get('fresh_until')}` (stale: `{value.get('stale')}`)",
             f"- Workflow: `{value.get('workflow_version')}`",
             f"- Correlation: `{value.get('correlation_id')}`", "", "## Evidence", ""]
    if isinstance(evidence, list | tuple):
        lines.extend(
            f"- `{json.dumps(item, ensure_ascii=False, sort_keys=True)}`" for item in evidence
        )
    return "\n".join(lines)


def _explanation(value: InsightExplanation) -> dict[str, object]:
    return value.insight.to_dict() | {"plan_id": value.plan_id, "decision_id": value.decision_id,
        "grant_id": value.grant_id, "task_id": value.task_id, "run_id": value.run_id}


def _plain(value: object) -> object:
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _error(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}}, sort_keys=True)


class _Prometheus:
    def __init__(self, value: str) -> None:
        self.value = value


def _command_row(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    value = dict(row)
    for name in ("args_json", "result_json"):
        raw = value.pop(name, None)
        value[name.removesuffix("_json")] = None if raw is None else json.loads(str(raw))
    return value


async def _run_runtime(
    database_path: Path, stdout: TextIO, stderr: TextIO, args: argparse.Namespace
) -> int:
    from active_agent_platform.scheduler import MissedTriggerPolicy
    from apps.quant_agent.runtime import DailyReviewSchedule, build_quant_runtime

    try:
        hour, minute = (int(value) for value in args.daily_review_at.split(":"))
        review_at = time(hour, minute)
        schedule = DailyReviewSchedule(
            at=review_at, timezone=args.daily_review_timezone,
            window_seconds=args.daily_review_window_seconds,
            max_missed_seconds=args.daily_review_max_missed_seconds,
            missed_policy=MissedTriggerPolicy(args.daily_review_missed_policy),
            trading_days_only=not args.daily_review_all_days,
        )
        default_schedule = DailyReviewSchedule()
        components = (
            build_quant_runtime(database_path)
            if schedule == default_schedule
            else build_quant_runtime(database_path, schedule=schedule)
        )
    except (TypeError, ValueError) as error:
        stderr.write(_error("RUNTIME_CONFIG_INVALID", str(error)) + "\n")
        return EXIT_USAGE
    await components.database.initialize()
    service = components.service
    engine = getattr(components, "engine", _LegacyEngineAdapter(service))
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()

    def request_stop() -> None:
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except NotImplementedError:
            pass
    try:
        serving = asyncio.create_task(engine.run(), name="quant-loop-engine")
        await engine.wait_started()
        snapshot = cast(Any, engine.health())
        if snapshot.system.value not in {"HEALTHY", "DEGRADED"}:
            engine.request_shutdown()
            await serving
            raise RuntimeError(f"LoopEngine failed to become ready: {snapshot.system.value}")
        stdout.write(json.dumps({
            "status": "READY", "database": str(database_path.resolve()),
            "service": service.name, "loop_instance": snapshot.instance_id,
            "loop_health": snapshot.system.value,
        }, sort_keys=True) + "\n")
        stdout.flush()
        stop_waiter = asyncio.create_task(stopped.wait(), name="quant-stop-waiter")
        done, _ = await asyncio.wait(
            (serving, stop_waiter), return_when=asyncio.FIRST_COMPLETED
        )
        if serving in done:
            await serving
        engine.request_shutdown()
        if not serving.done():
            await serving
        if not stop_waiter.done():
            stop_waiter.cancel()
        return EXIT_OK
    except (OSError, RuntimeError, ValueError) as error:
        stderr.write(_error("RUNTIME_FAILED", str(error)) + "\n")
        return EXIT_UNAVAILABLE
    finally:
        await components.database.close()


def main() -> int:
    import sys
    arguments = list(sys.argv[1:])
    commands = {
        "shell", "start", "run", "status", "system", "brain", "loop", "events", "plans",
        "tasks", "catalog", "skills", "workflows", "health", "diagnose", "metrics", "stop",
        "inject", "market", "subscriptions", "replay", "log", "commands", "insights",
    }
    if not any(value in commands for value in arguments):
        arguments.append("shell")
    try:
        return asyncio.run(run(arguments, sys.stdout, sys.stderr, sys.stdin))
    except KeyboardInterrupt:
        sys.stdout.write("\nBIA terminal stopped.\n")
        return 130
