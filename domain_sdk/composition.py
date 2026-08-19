"""Application composition boundary connecting plugins to the generic platform."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from active_agent_platform.foundation import RuntimeDependencies
from active_agent_platform.runtime import HealthSnapshot, LoopEngine
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.contracts import DomainPlugin
from domain_sdk.registry import PluginCatalog


@dataclass(slots=True)
class Application:
    catalog: PluginCatalog
    database: SQLiteDatabase
    engine: LoopEngine

    async def run(self) -> None:
        await self.database.initialize()
        try:
            await self.engine.run()
        finally:
            await self.database.close()

    def request_shutdown(self) -> None:
        self.engine.request_shutdown()

    def health(self) -> HealthSnapshot:
        return self.engine.health()


class CompositionRoot:
    def __init__(
        self,
        dependencies: RuntimeDependencies,
        database_path: str | Path,
        plugins: Iterable[DomainPlugin],
        *,
        critical_services: frozenset[str] = frozenset(),
    ) -> None:
        self._dependencies = dependencies
        self._database_path = database_path
        self._plugins = tuple(plugins)
        self._critical_services = critical_services

    def build(self) -> Application:
        catalog = PluginCatalog.from_plugins(self._plugins)
        database = SQLiteDatabase(self._database_path)
        engine = LoopEngine(
            self._dependencies,
            catalog.services,
            critical_services=self._critical_services,
        )
        return Application(catalog=catalog, database=database, engine=engine)
