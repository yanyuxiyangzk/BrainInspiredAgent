from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from active_agent_platform.events import (
    PersistedBusMessage,
    TransactionalInboxConsumer,
)
from active_agent_platform.foundation import FakeClock, SystemClock, Uuid7Generator
from active_agent_platform.storage import SQLiteDatabase
from apps.brainagent_cli import run as run_brainagent
from apps.quant_agent.cli import EXIT_OK, EXIT_UNAVAILABLE
from apps.quant_agent.cli import run as run_cli
from apps.quant_agent.plugin import QuantDomainPlugin
from apps.quant_agent.runtime import _CommandMessage, _CommandPublisher, build_quant_runtime


async def _cli(*args: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(args, stdout, stderr)
    assert not stderr.getvalue()
    return code, json.loads(stdout.getvalue())


@pytest.mark.asyncio
async def test_q01_quant_plugin_is_discoverable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = await run_brainagent((
        "--database", str(tmp_path / "catalog.db"),
        "--plugin", "apps.quant_agent.plugin:QuantDomainPlugin", "status",
    ))
    assert code == 0
    value = json.loads(capsys.readouterr().out)
    assert value == {"plugins": ["quant_agent"], "capabilities": 3, "skills": 3,
                     "workflows": 2}


@pytest.mark.asyncio
async def test_q01_catalog_adapter_cannot_bypass_governed_runtime() -> None:
    adapter = QuantDomainPlugin().contribute().skills[0].adapter
    with pytest.raises(RuntimeError, match="governed"):
        await adapter.invoke({})


@pytest.mark.asyncio
async def test_q02_q03_q06_durable_command_runs_and_delivers(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "facts.db"
    code, started = await _cli("--database", str(path), "start")
    assert code == EXIT_OK and started["status"] == "READY" and path.exists()
    code, subscribed = await _cli(
        "--database", str(path), "subscriptions", "add", "local-user"
    )
    assert code == EXIT_OK and subscribed["status"] == "SUBSCRIBED"
    code, published = await _cli(
        "--database", str(path), "market", "summary",
        "--symbols", "INDEX.TEST,INDEX.DEMO", "--title", "Runtime E2E",
    )
    assert code == EXIT_OK and published["status"] == "PUBLISHED"

    # The command survives being written before the long-lived worker starts.
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    assert (await components.service._relay.publish_due()).published == 1
    assert await components.service.process_one()
    await components.service.stop()
    await components.database.close()

    code, commands = await _cli("--database", str(path), "commands")
    command = commands["commands"][0]  # type: ignore[index]
    assert command["status"] == "SUCCEEDED"  # type: ignore[index]
    assert command["correlation_id"] == published["message_id"]  # type: ignore[index]
    code, insights = await _cli("--database", str(path), "insights", "latest")
    insight = insights["insights"][0]  # type: ignore[index]
    assert insight["title"] == "Runtime E2E"  # type: ignore[index]
    assert len(insight["evidence"]) == 2  # type: ignore[index]
    code, deliveries = await _cli(
        "--database", str(path), "subscriptions", "list", "local-user"
    )
    assert len(deliveries["deliveries"]) == 1  # type: ignore[arg-type]

    # Same business key is acknowledged without another execution or notification.
    await _cli(
        "--database", str(path), "market", "summary",
        "--symbols", "INDEX.TEST,INDEX.DEMO", "--title", "Runtime E2E",
    )
    restarted = build_quant_runtime(path)
    await restarted.database.initialize()
    await restarted.service.start()
    assert (await restarted.service._relay.publish_due()).published >= 1
    assert not await restarted.service.process_one()
    await restarted.service.stop()
    counts = await restarted.database.fetch_one(
        "SELECT (SELECT count(*) FROM command_execution),"
        "(SELECT count(*) FROM task),(SELECT count(*) FROM local_notification_delivery),"
        "(SELECT count(*) FROM insight_delivery)"
    )
    assert counts is not None and tuple(counts) == (1, 1, 1, 1)
    await restarted.database.close()


@pytest.mark.asyncio
async def test_q07_running_command_is_recovered_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "recovery.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    now = "2026-08-19T00:00:00Z"
    async with database.transaction() as transaction:
        await transaction.execute(
            """INSERT INTO command_execution(
                   command_id,dedup_key,command,args_json,status,accepted_at,
                   started_at,correlation_id,attempt
               ) VALUES ('cmd','key','market.summary','{}','RUNNING',?,?, 'corr',1)""",
            (now, now),
        )
    await database.close()
    components = build_quant_runtime(path)
    await components.database.initialize()
    await components.service.start()
    row = await components.database.fetch_one(
        "SELECT status,started_at FROM command_execution WHERE command_id='cmd'"
    )
    assert row is not None and tuple(row) == ("ACCEPTED", None)
    await components.service.stop()
    await components.database.close()


@pytest.mark.asyncio
async def test_q07_service_loop_stops_cleanly(tmp_path: Path) -> None:
    components = build_quant_runtime(tmp_path / "loop.db")
    await components.database.initialize()
    await components.service.start()
    serving = asyncio.create_task(components.service.serve())
    await components.service.quiesce()
    await components.service.stop()
    await asyncio.wait_for(serving, timeout=1)
    await components.database.close()


@pytest.mark.asyncio
async def test_q07_service_loop_polls_and_checkpoints(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 8, 20, 0, 0, tzinfo=UTC))
    components = build_quant_runtime(tmp_path / "poll.db", clock=clock)
    await components.database.initialize()
    await components.service.start()
    serving = asyncio.create_task(components.service.serve())
    for _ in range(2):
        await asyncio.sleep(0)
        clock.advance(0.1)
    await components.service.checkpoint()
    snapshot = await components.service.operational_snapshot()
    assert snapshot == {"lag": {"commands": 0, "outbox": 0}, "checkpoints": []}
    async with components.database.transaction() as transaction:
        await transaction.execute(
            "INSERT INTO schedule_checkpoint(schedule_id,occurrence_key,status,consumed_at) "
            "VALUES ('demo','2026-08-20','FIRED','2026-08-20T00:00:00Z')"
        )
    populated = await components.service.operational_snapshot()
    assert populated["checkpoints"]
    await components.service.stop()
    clock.advance(0.1)
    await asyncio.wait_for(serving, timeout=1)
    await components.database.close()


def test_q02_rejects_malformed_command_envelopes() -> None:
    base = {"correlation_id": "c", "dedup_key": "d"}
    with pytest.raises(TypeError, match="payload"):
        _CommandMessage(PersistedBusMessage("m", "command.received", "test", 1, base))
    with pytest.raises(TypeError, match="data"):
        _CommandMessage(PersistedBusMessage(
            "m", "command.received", "test", 1, base | {"payload": {}}
        ))
    with pytest.raises(TypeError, match="args"):
        _CommandMessage(PersistedBusMessage(
            "m", "command.received", "test", 1,
            base | {"payload": {"data": {"command": "x", "args": []}}},
        ))


@pytest.mark.asyncio
async def test_q02_filters_non_command_and_checks_message_type() -> None:
    class Consumer:
        async def consume(self, message: object, handler: object) -> object:
            raise AssertionError((message, handler))

    publisher = _CommandPublisher(Consumer())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="persisted"):
        await publisher.publish(object())
    report = await publisher.publish(PersistedBusMessage("m", "fact.created", "test", 1, {}))
    assert report.deliveries[0].outcome.value == "FILTERED"


@pytest.mark.asyncio
async def test_q02_unsupported_command_is_rejected_and_dead_lettered(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "unsupported.db")
    await database.initialize()
    clock = SystemClock()
    consumer = TransactionalInboxConsumer(
        "quant-command", database, clock, Uuid7Generator(clock)
    )
    publisher = _CommandPublisher(consumer)
    message = PersistedBusMessage(
        "unsupported", "command.received", "test", 1,
        {"correlation_id": "unsupported", "dedup_key": "unsupported",
         "payload": {"data": {"command": "trade.execute", "args": {}}}},
    )
    report = await publisher.publish(message)
    assert report.deliveries[0].outcome.value == "REJECTED"
    row = await database.fetch_one(
        "SELECT status FROM inbox_message WHERE msg_id='unsupported'"
    )
    assert row is not None and row["status"] == "DEAD_LETTER"
    await database.close()


@pytest.mark.asyncio
async def test_q03_failure_is_durable_and_empty_queue_is_idle(tmp_path: Path) -> None:
    components = build_quant_runtime(tmp_path / "failure.db")
    await components.database.initialize()
    await components.service.start()
    assert not await components.service.process_one()
    now = "2026-08-19T00:00:00Z"
    async with components.database.transaction() as transaction:
        await transaction.execute(
            """INSERT INTO command_execution(
                   command_id,dedup_key,command,args_json,status,accepted_at,correlation_id
               ) VALUES ('bad','bad-key','market.summary',?,'ACCEPTED',?,'bad-corr')""",
            (json.dumps({"symbols": [1]}), now),
        )
    assert await components.service.process_one()
    row = await components.database.fetch_one(
        "SELECT status,error_code FROM command_execution WHERE command_id='bad'"
    )
    assert row is not None and row["status"] == "FAILED" and row["error_code"]
    await components.service.stop()
    await components.database.close()


@pytest.mark.asyncio
async def test_q07_run_cli_handles_runtime_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.quant_agent import runtime as runtime_module

    database = SQLiteDatabase(tmp_path / "failed-run.db")

    class FailedService:
        name = "failed"

        async def start(self) -> None:
            raise RuntimeError("start failed")

    class FailedComponents:
        def __init__(self) -> None:
            self.database = database
            self.service = FailedService()

    monkeypatch.setattr(runtime_module, "build_quant_runtime", lambda path: FailedComponents())
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(("--database", str(tmp_path / "failed-run.db"), "run"), stdout, stderr)
    assert code == EXIT_UNAVAILABLE and "RUNTIME_FAILED" in stderr.getvalue()


@pytest.mark.asyncio
async def test_q07_run_cli_serves_and_drains_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.quant_agent import runtime as runtime_module

    database = SQLiteDatabase(tmp_path / "clean-run.db")
    calls: list[str] = []

    class CleanService:
        name = "clean"

        async def start(self) -> None:
            calls.append("start")

        async def serve(self) -> None:
            calls.append("serve")

        async def quiesce(self) -> None:
            calls.append("quiesce")

        async def checkpoint(self) -> None:
            calls.append("checkpoint")

        async def stop(self) -> None:
            calls.append("stop")

    class CleanComponents:
        def __init__(self) -> None:
            self.database = database
            self.service = CleanService()

    monkeypatch.setattr(runtime_module, "build_quant_runtime", lambda path: CleanComponents())
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(("--database", str(tmp_path / "clean-run.db"), "run"), stdout, stderr)
    assert code == EXIT_OK and not stderr.getvalue()
    assert json.loads(stdout.getvalue())["status"] == "READY"
    assert calls == ["start", "serve", "quiesce", "checkpoint", "stop"]


@pytest.mark.asyncio
async def test_q07_command_query_errors_are_machine_readable(tmp_path: Path) -> None:
    path = tmp_path / "query.db"
    await _cli("--database", str(path), "start")
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(path), "commands", "missing"), stdout, stderr
    )
    assert code == 4 and "NOT_FOUND" in stderr.getvalue()
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(path), "commands", "--limit", "0"), stdout, stderr
    )
    assert code == 2 and "INVALID_ARGUMENT" in stderr.getvalue()
    stdout, stderr = StringIO(), StringIO()
    code = await run_cli(
        ("--database", str(path), "inject", "status", "--args", "[]"), stdout, stderr
    )
    assert code == 2 and "JSON object" in stderr.getvalue()


def test_q08_external_cli_and_runtime_processes(tmp_path: Path) -> None:
    database = tmp_path / "blackbox" / "facts.db"
    runtime = subprocess.Popen(
        [sys.executable, "-m", "apps.quant_agent", "--database", str(database), "run"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert runtime.stdout is not None
        ready = json.loads(runtime.stdout.readline())
        assert ready["status"] == "READY"
        submitted = subprocess.run(
            [sys.executable, "-m", "apps.quant_agent", "--database", str(database),
             "market", "summary", "--symbols", "INDEX.BLACKBOX",
             "--title", "Blackbox summary"],
            check=True, capture_output=True, text=True,
        )
        message_id = json.loads(submitted.stdout)["message_id"]
        command: dict[str, object] = {}
        for _ in range(50):
            queried = subprocess.run(
                [sys.executable, "-m", "apps.quant_agent", "--database", str(database),
                 "commands", message_id],
                check=False, capture_output=True, text=True,
            )
            if queried.returncode == 0:
                command = json.loads(queried.stdout)
                if command["status"] in {"SUCCEEDED", "FAILED"}:
                    break
            time.sleep(0.05)
        assert command["status"] == "SUCCEEDED"
        insights = subprocess.run(
            [sys.executable, "-m", "apps.quant_agent", "--database", str(database),
             "insights", "latest"],
            check=True, capture_output=True, text=True,
        )
        latest = json.loads(insights.stdout)["insights"]
        assert len(latest) == 1 and latest[0]["correlation_id"] == message_id
    finally:
        runtime.terminate()
        runtime.wait(timeout=5)
        assert runtime.returncode == 0
