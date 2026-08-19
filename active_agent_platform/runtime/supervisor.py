"""Fault-isolating lifecycle supervision for long-lived services."""

import asyncio
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from active_agent_platform.errors import ErrorCode
from brain_kernel.lifecycle import ManagedService
from brain_kernel.ports import Clock, LogLevel, StructuredLogger


class ServiceState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    RESTARTING = "RESTARTING"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    STOPPED = "STOPPED"


class SystemHealth(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SAFE = "SAFE"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    crash_limit: int = 3
    crash_window_seconds: float = 60.0
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 30.0
    shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.crash_limit < 1:
            raise ValueError("crash_limit must be positive")
        for name, value in (
            ("crash_window_seconds", self.crash_window_seconds),
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("maximum_backoff_seconds", self.maximum_backoff_seconds),
            ("shutdown_timeout_seconds", self.shutdown_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.initial_backoff_seconds > self.maximum_backoff_seconds:
            raise ValueError("initial_backoff_seconds must not exceed maximum_backoff_seconds")


@dataclass(frozen=True, slots=True)
class ServiceRegistration:
    service: ManagedService
    critical: bool = False


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    instance_id: str
    system: SystemHealth
    services: dict[str, ServiceState]


class Supervisor:
    def __init__(
        self,
        *,
        instance_id: str,
        registrations: tuple[ServiceRegistration, ...],
        clock: Clock,
        logger: StructuredLogger,
        config: SupervisorConfig,
    ) -> None:
        names = [registration.service.name for registration in registrations]
        if len(names) != len(set(names)):
            raise ValueError("service names must be unique")
        self._instance_id = instance_id
        self._registrations = registrations
        self._clock = clock
        self._logger = logger
        self._config = config
        self._states = {name: ServiceState.STOPPED for name in names}
        self._crashes = {name: deque[float]() for name in names}
        self._shutdown_requested = asyncio.Event()
        self._startup_complete = asyncio.Event()
        self._stopped = asyncio.Event()
        self._system_health = SystemHealth.STARTING
        self._running = False
        self._has_run = False

    async def run(self) -> None:
        if self._has_run:
            raise RuntimeError("supervisor instances can only run once")
        self._has_run = True
        self._running = True
        try:
            async with asyncio.TaskGroup() as task_group:
                service_tasks: set[asyncio.Task[None]] = set()
                for registration in self._registrations:
                    started = await self._start(registration)
                    if started:
                        service_tasks.add(
                            task_group.create_task(
                                self._supervise(registration),
                                name=f"service:{registration.service.name}",
                            )
                        )
                    else:
                        service_tasks.add(
                            task_group.create_task(
                                self._restart_after_failure(registration),
                                name=f"service:{registration.service.name}",
                            )
                        )
                self._refresh_health()
                self._startup_complete.set()
                await self._shutdown_requested.wait()
                self._system_health = SystemHealth.SHUTTING_DOWN
                await self._shutdown(service_tasks)
        finally:
            self._system_health = SystemHealth.STOPPED
            self._running = False
            self._stopped.set()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()

    async def wait_started(self) -> None:
        await self._startup_complete.wait()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    def snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(
            instance_id=self._instance_id,
            system=self._system_health,
            services=dict(self._states),
        )

    async def _start(self, registration: ServiceRegistration) -> bool:
        name = registration.service.name
        self._states[name] = ServiceState.STARTING
        try:
            await registration.service.start()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - isolate untrusted service failures.
            self._record_crash(registration, error)
            return False
        self._states[name] = ServiceState.READY
        self._logger.emit(LogLevel.INFO, "service.ready", service=name)
        self._refresh_health()
        return True

    async def _supervise(self, registration: ServiceRegistration) -> None:
        name = registration.service.name
        while not self._shutdown_requested.is_set():
            try:
                await registration.service.serve()
                if self._shutdown_requested.is_set():
                    break
                raise RuntimeError("service exited unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - keep sibling services alive.
                if self._record_crash(registration, error):
                    break
                await self._backoff(name)
                if self._shutdown_requested.is_set():
                    break
                if not await self._start(registration):
                    if self._states[name] is ServiceState.CIRCUIT_OPEN:
                        break
                    continue
        if self._states[name] is not ServiceState.CIRCUIT_OPEN:
            self._states[name] = ServiceState.STOPPED

    async def _restart_after_failure(self, registration: ServiceRegistration) -> None:
        name = registration.service.name
        while self._states[name] is not ServiceState.CIRCUIT_OPEN:
            await self._backoff(name)
            if self._shutdown_requested.is_set():
                self._states[name] = ServiceState.STOPPED
                return
            if await self._start(registration):
                await self._supervise(registration)
                return

    def _record_crash(self, registration: ServiceRegistration, error: Exception) -> bool:
        name = registration.service.name
        now = self._clock.monotonic()
        crashes = self._crashes[name]
        crashes.append(now)
        cutoff = now - self._config.crash_window_seconds
        while crashes and crashes[0] < cutoff:
            crashes.popleft()
        self._logger.emit(
            LogLevel.ERROR,
            "service.crashed",
            service=name,
            error_code=ErrorCode.AREA_CRASHED.value,
            error_type=type(error).__name__,
            crash_count=len(crashes),
        )
        if registration.critical or len(crashes) >= self._config.crash_limit:
            self._states[name] = ServiceState.CIRCUIT_OPEN
            self._system_health = (
                SystemHealth.SAFE if registration.critical else SystemHealth.DEGRADED
            )
            self._logger.emit(
                LogLevel.CRITICAL,
                "service.circuit_opened",
                service=name,
                critical=registration.critical,
                crash_count=len(crashes),
            )
            return True
        self._states[name] = ServiceState.RESTARTING
        self._refresh_health()
        return False

    async def _backoff(self, name: str) -> None:
        attempt = max(1, len(self._crashes[name]))
        delay = min(
            self._config.initial_backoff_seconds * (2 ** (attempt - 1)),
            self._config.maximum_backoff_seconds,
        )
        await self._clock.sleep(delay)

    async def _shutdown(self, service_tasks: set[asyncio.Task[None]]) -> None:
        for registration in reversed(self._registrations):
            await self._safe_call(registration, "quiesce", registration.service.quiesce())

        timeout_task = asyncio.create_task(
            self._clock.sleep(self._config.shutdown_timeout_seconds),
            name="supervisor:shutdown-timeout",
        )
        if service_tasks:
            drain_task = asyncio.create_task(
                self._wait_for_tasks(service_tasks), name="supervisor:drain"
            )
            done, _ = await asyncio.wait(
                {drain_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if timeout_task in done:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
            else:
                timeout_task.cancel()
                await asyncio.gather(timeout_task, return_exceptions=True)
        else:
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)

        for registration in reversed(self._registrations):
            await self._safe_call(registration, "checkpoint", registration.service.checkpoint())
        for task in service_tasks:
            if not task.done():
                task.cancel()
        if service_tasks:
            await asyncio.gather(*service_tasks, return_exceptions=True)
        for registration in reversed(self._registrations):
            await self._safe_call(registration, "stop", registration.service.stop())
            if self._states[registration.service.name] is not ServiceState.CIRCUIT_OPEN:
                self._states[registration.service.name] = ServiceState.STOPPED

    @staticmethod
    async def _wait_for_tasks(tasks: set[asyncio.Task[None]]) -> None:
        await asyncio.gather(
            *(asyncio.shield(task) for task in tasks), return_exceptions=True
        )

    async def _safe_call(
        self,
        registration: ServiceRegistration,
        phase: str,
        operation: Coroutine[Any, Any, None],
    ) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - shutdown must continue for other services.
            self._logger.emit(
                LogLevel.ERROR,
                "service.shutdown_failed",
                service=registration.service.name,
                phase=phase,
                error_type=type(error).__name__,
            )

    def _refresh_health(self) -> None:
        if self._system_health in {SystemHealth.SAFE, SystemHealth.SHUTTING_DOWN}:
            return
        if any(state is ServiceState.CIRCUIT_OPEN for state in self._states.values()):
            self._system_health = SystemHealth.DEGRADED
        elif all(state is ServiceState.READY for state in self._states.values()):
            self._system_health = SystemHealth.HEALTHY
        else:
            self._system_health = SystemHealth.STARTING
