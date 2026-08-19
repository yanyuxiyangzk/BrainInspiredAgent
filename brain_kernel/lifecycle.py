"""Lifecycle contract for services managed by the platform supervisor."""

from typing import Protocol


class ManagedService(Protocol):
    """A restartable, long-lived service with explicit shutdown phases."""

    @property
    def name(self) -> str:
        """Return the stable service name used in health and diagnostics."""

    async def start(self) -> None:
        """Initialize dependencies and become ready to serve."""

    async def serve(self) -> None:
        """Serve until quiesced or cancelled."""

    async def quiesce(self) -> None:
        """Stop accepting new work while allowing accepted work to drain."""

    async def checkpoint(self) -> None:
        """Persist the minimum state required for recovery."""

    async def stop(self) -> None:
        """Release resources; repeated calls must be safe."""
