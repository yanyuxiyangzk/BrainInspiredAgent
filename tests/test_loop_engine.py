import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from active_agent_platform.foundation import (
    CapturingLogger,
    FakeClock,
    FakeUuidGenerator,
    RuntimeDependencies,
    Settings,
)
from active_agent_platform.runtime import (
    LoopEngine,
    ServiceState,
    SupervisorConfig,
    SystemHealth,
)

START = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


class FakeService:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        serve_failures: int = 0,
        start_failures: int = 0,
        drain_on_quiesce: bool = True,
        shutdown_failures: frozenset[str] = frozenset(),
    ) -> None:
        self._name = name
        self._events = events
        self._serve_failures = serve_failures
        self._start_failures = start_failures
        self._drain_on_quiesce = drain_on_quiesce
        self._shutdown_failures = shutdown_failures
        self._release = asyncio.Event()
        self.serve_attempts = 0

    def release(self) -> None:
        self._release.set()

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self._events.append(f"start:{self.name}")
        if self._start_failures:
            self._start_failures -= 1
            raise RuntimeError("start failure")

    async def serve(self) -> None:
        self.serve_attempts += 1
        self._events.append(f"serve:{self.name}:{self.serve_attempts}")
        if self._serve_failures:
            self._serve_failures -= 1
            raise RuntimeError("serve failure")
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self._events.append(f"cancel:{self.name}")
            raise

    async def quiesce(self) -> None:
        self._events.append(f"quiesce:{self.name}")
        if "quiesce" in self._shutdown_failures:
            raise RuntimeError("quiesce failure")
        if self._drain_on_quiesce:
            self._release.set()

    async def checkpoint(self) -> None:
        self._events.append(f"checkpoint:{self.name}")
        if "checkpoint" in self._shutdown_failures:
            raise RuntimeError("checkpoint failure")

    async def stop(self) -> None:
        self._events.append(f"stop:{self.name}")
        self._release.set()
        if "stop" in self._shutdown_failures:
            raise RuntimeError("stop failure")


def dependencies(clock: FakeClock, logger: CapturingLogger) -> RuntimeDependencies:
    return RuntimeDependencies(
        settings=Settings(shutdown_timeout_seconds=5),
        clock=clock,
        uuid=FakeUuidGenerator([UUID("00000000-0000-0000-0000-000000000123")]),
        logger=logger,
    )


async def cycle(count: int = 3) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_startup_health_and_reverse_graceful_shutdown() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    logger = CapturingLogger()
    first = FakeService("memory", events)
    second = FakeService("bus", events)
    engine = LoopEngine(dependencies(clock, logger), [first, second])

    runner = asyncio.create_task(engine.run(), name="test:engine")
    await engine.wait_started()
    snapshot = engine.health()

    assert snapshot.instance_id == "00000000-0000-0000-0000-000000000123"
    assert snapshot.system is SystemHealth.HEALTHY
    assert snapshot.services == {
        "memory": ServiceState.READY,
        "bus": ServiceState.READY,
    }
    assert events[:2] == ["start:memory", "start:bus"]

    engine.request_shutdown()
    await runner
    assert events.index("quiesce:bus") < events.index("quiesce:memory")
    assert events.index("checkpoint:bus") < events.index("checkpoint:memory")
    assert events.index("stop:bus") < events.index("stop:memory")
    assert engine.health().system is SystemHealth.STOPPED


@pytest.mark.asyncio
async def test_noncritical_crash_is_isolated_and_restarted() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    logger = CapturingLogger()
    unstable = FakeService("unstable", events, serve_failures=1)
    healthy = FakeService("healthy", events)
    engine = LoopEngine(
        dependencies(clock, logger),
        [unstable, healthy],
        supervisor_config=SupervisorConfig(initial_backoff_seconds=1),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    await cycle()
    assert engine.health().services["unstable"] is ServiceState.RESTARTING
    assert engine.health().services["healthy"] is ServiceState.READY

    clock.advance(1)
    await cycle()
    assert unstable.serve_attempts == 2
    assert engine.health().system is SystemHealth.HEALTHY
    assert any(record.fields.get("error_code") == "AREA_CRASHED" for record in logger.records)

    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_three_crashes_open_circuit_and_degrade_system() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    logger = CapturingLogger()
    failing = FakeService("failing", events, serve_failures=3)
    engine = LoopEngine(
        dependencies(clock, logger),
        [failing],
        supervisor_config=SupervisorConfig(
            crash_limit=3,
            crash_window_seconds=60,
            initial_backoff_seconds=1,
            maximum_backoff_seconds=5,
        ),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    await cycle()
    clock.advance(1)
    await cycle()
    clock.advance(2)
    await cycle()

    snapshot = engine.health()
    assert failing.serve_attempts == 3
    assert snapshot.services["failing"] is ServiceState.CIRCUIT_OPEN
    assert snapshot.system is SystemHealth.DEGRADED
    circuit_logs = [record for record in logger.records if record.event == "service.circuit_opened"]
    assert len(circuit_logs) == 1

    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_critical_service_crash_enters_safe_without_restart() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    failing = FakeService("storage", events, serve_failures=1)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [failing],
        critical_services=frozenset({"storage"}),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    await cycle()
    assert engine.health().system is SystemHealth.SAFE
    assert failing.serve_attempts == 1

    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_shutdown_timeout_checkpoints_before_cancel_and_leaves_no_runtime_tasks() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    stubborn = FakeService("stubborn", events, drain_on_quiesce=False)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [stubborn],
        supervisor_config=SupervisorConfig(shutdown_timeout_seconds=5),
    )

    runner = asyncio.create_task(engine.run(), name="test:engine")
    await engine.wait_started()
    engine.request_shutdown()
    await cycle()
    clock.advance(5)
    await runner

    assert events.index("checkpoint:stubborn") < events.index("cancel:stubborn")
    assert events.index("cancel:stubborn") < events.index("stop:stubborn")
    runtime_tasks = [
        task.get_name()
        for task in asyncio.all_tasks()
        if not task.done()
        and task is not asyncio.current_task()
        and task.get_name().startswith(("service:", "supervisor:"))
    ]
    assert runtime_tasks == []


def test_duplicate_service_names_are_rejected() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    with pytest.raises(ValueError, match="unique"):
        LoopEngine(
            dependencies(clock, CapturingLogger()),
            [FakeService("same", events), FakeService("same", events)],
        )


@pytest.mark.asyncio
async def test_engine_instance_cannot_run_twice() -> None:
    clock = FakeClock(START)
    engine = LoopEngine(dependencies(clock, CapturingLogger()))
    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    engine.request_shutdown()
    await runner

    with pytest.raises(RuntimeError, match="only run once"):
        await engine.run()


@pytest.mark.asyncio
async def test_initial_start_failure_retries_and_becomes_healthy() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    service = FakeService("delayed", events, start_failures=1)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [service],
        supervisor_config=SupervisorConfig(initial_backoff_seconds=1),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    assert engine.health().services["delayed"] is ServiceState.RESTARTING
    clock.advance(1)
    await cycle()
    assert engine.health().system is SystemHealth.HEALTHY
    assert events.count("start:delayed") == 2

    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_old_crashes_expire_from_circuit_window() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    service = FakeService("delayed", events, start_failures=2)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [service],
        supervisor_config=SupervisorConfig(
            crash_limit=2,
            crash_window_seconds=60,
            initial_backoff_seconds=1,
        ),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    clock.advance(61)
    await cycle()
    assert engine.health().services["delayed"] is ServiceState.RESTARTING
    clock.advance(2)
    await cycle()
    assert engine.health().system is SystemHealth.HEALTHY

    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_shutdown_hook_failures_are_logged_and_do_not_block_exit() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    logger = CapturingLogger()
    service = FakeService(
        "faulty-shutdown",
        events,
        drain_on_quiesce=False,
        shutdown_failures=frozenset({"quiesce", "checkpoint", "stop"}),
    )
    engine = LoopEngine(
        dependencies(clock, logger),
        [service],
        supervisor_config=SupervisorConfig(shutdown_timeout_seconds=5),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    engine.request_shutdown()
    await cycle()
    clock.advance(5)
    await engine.wait_stopped()
    await runner

    phases = {
        record.fields["phase"]
        for record in logger.records
        if record.event == "service.shutdown_failed"
    }
    assert phases == {"quiesce", "checkpoint", "stop"}


@pytest.mark.asyncio
async def test_empty_engine_can_be_stopped_before_run() -> None:
    clock = FakeClock(START)
    engine = LoopEngine(dependencies(clock, CapturingLogger()))
    engine.request_shutdown()
    await engine.run()
    assert engine.health().system is SystemHealth.STOPPED


@pytest.mark.asyncio
async def test_unexpected_normal_service_exit_is_treated_as_crash() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    logger = CapturingLogger()
    service = FakeService("returning", events)
    service.release()
    engine = LoopEngine(
        dependencies(clock, logger),
        [service],
        supervisor_config=SupervisorConfig(
            crash_limit=3,
            initial_backoff_seconds=1,
            maximum_backoff_seconds=5,
        ),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    await cycle()
    clock.advance(1)
    await cycle()
    clock.advance(2)
    await cycle()

    assert service.serve_attempts == 3
    assert engine.health().services["returning"] is ServiceState.CIRCUIT_OPEN
    crash_types = {
        record.fields["error_type"]
        for record in logger.records
        if record.event == "service.crashed"
    }
    assert crash_types == {"RuntimeError"}
    engine.request_shutdown()
    await runner


@pytest.mark.asyncio
async def test_shutdown_during_crash_backoff_prevents_restart() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    service = FakeService("backing-off", events, serve_failures=1)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [service],
        supervisor_config=SupervisorConfig(initial_backoff_seconds=1),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    await cycle()
    assert engine.health().services["backing-off"] is ServiceState.RESTARTING
    engine.request_shutdown()
    await cycle()
    clock.advance(1)
    await runner

    assert events.count("start:backing-off") == 1


@pytest.mark.asyncio
async def test_shutdown_during_initial_start_backoff_prevents_retry() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    service = FakeService("not-started", events, start_failures=1)
    engine = LoopEngine(
        dependencies(clock, CapturingLogger()),
        [service],
        supervisor_config=SupervisorConfig(initial_backoff_seconds=1),
    )

    runner = asyncio.create_task(engine.run())
    await engine.wait_started()
    engine.request_shutdown()
    await cycle()
    clock.advance(1)
    await runner

    assert events.count("start:not-started") == 1
    assert engine.health().services["not-started"] is ServiceState.STOPPED


@pytest.mark.asyncio
async def test_shutdown_requested_before_run_still_closes_registered_service() -> None:
    events: list[str] = []
    clock = FakeClock(START)
    service = FakeService("pre-stopped", events)
    engine = LoopEngine(dependencies(clock, CapturingLogger()), [service])
    engine.request_shutdown()

    await engine.run()

    assert events == [
        "start:pre-stopped",
        "quiesce:pre-stopped",
        "checkpoint:pre-stopped",
        "stop:pre-stopped",
    ]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"crash_limit": 0}, "crash_limit"),
        ({"crash_window_seconds": 0}, "crash_window_seconds"),
        ({"initial_backoff_seconds": 0}, "initial_backoff_seconds"),
        ({"maximum_backoff_seconds": 0}, "maximum_backoff_seconds"),
        ({"shutdown_timeout_seconds": 0}, "shutdown_timeout_seconds"),
        ({"initial_backoff_seconds": 2, "maximum_backoff_seconds": 1}, "must not exceed"),
    ],
)
def test_supervisor_config_rejects_invalid_values(
    kwargs: dict[str, int | float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SupervisorConfig(**kwargs)  # type: ignore[arg-type]
