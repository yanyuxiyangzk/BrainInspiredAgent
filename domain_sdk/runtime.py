"""Public builder for a domain-neutral BrainAgent application."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from active_agent_platform.foundation import (
    CapturingLogger,
    RuntimeDependencies,
    Settings,
    SystemClock,
    Uuid7Generator,
)
from domain_sdk.composition import Application, CompositionRoot
from domain_sdk.contracts import DomainPlugin


class RuntimeBuilder:
    """Build a platform runtime without importing any application package."""

    def __init__(self, database_path: str | Path = "brainagent.db") -> None:
        self._database_path = database_path
        self._plugins: list[DomainPlugin] = []
        self._dependencies: RuntimeDependencies | None = None

    def with_plugin(self, plugin: DomainPlugin) -> RuntimeBuilder:
        self._plugins.append(plugin)
        return self

    def with_plugins(self, plugins: Iterable[DomainPlugin]) -> RuntimeBuilder:
        self._plugins.extend(plugins)
        return self

    def with_dependencies(self, dependencies: RuntimeDependencies) -> RuntimeBuilder:
        self._dependencies = dependencies
        return self

    def build(self) -> Application:
        dependencies = self._dependencies
        if dependencies is None:
            clock = SystemClock()
            dependencies = RuntimeDependencies(
                settings=Settings(environment="production"),
                clock=clock,
                uuid=Uuid7Generator(clock),
                logger=CapturingLogger(),
            )
        return CompositionRoot(dependencies, self._database_path, self._plugins).build()
