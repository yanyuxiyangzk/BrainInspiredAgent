"""The process-level owner of runtime lifecycle and scheduling services."""

from collections.abc import Iterable

from active_agent_platform.foundation import RuntimeDependencies
from active_agent_platform.runtime.supervisor import (
    HealthSnapshot,
    ServiceRegistration,
    Supervisor,
    SupervisorConfig,
)
from brain_kernel.lifecycle import ManagedService


class LoopEngine:
    """Run all managed services on the caller's single asyncio event loop."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
        services: Iterable[ManagedService] = (),
        *,
        critical_services: frozenset[str] = frozenset(),
        supervisor_config: SupervisorConfig | None = None,
    ) -> None:
        registrations = tuple(
            ServiceRegistration(service, critical=service.name in critical_services)
            for service in services
        )
        config = supervisor_config or SupervisorConfig(
            shutdown_timeout_seconds=dependencies.settings.shutdown_timeout_seconds
        )
        self._supervisor = Supervisor(
            instance_id=str(dependencies.uuid.new()),
            registrations=registrations,
            clock=dependencies.clock,
            logger=dependencies.logger,
            config=config,
        )

    async def run(self) -> None:
        """Run until shutdown is requested; this method owns no nested event loop."""
        await self._supervisor.run()

    async def wait_started(self) -> None:
        await self._supervisor.wait_started()

    async def wait_stopped(self) -> None:
        await self._supervisor.wait_stopped()

    def request_shutdown(self) -> None:
        self._supervisor.request_shutdown()

    def health(self) -> HealthSnapshot:
        return self._supervisor.snapshot()
