"""SQLite-backed EventSink used by the CLI command adapter."""

from active_agent_platform.events import EventEnvelope, OutboxWriter
from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock


class SQLiteEventSink:
    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database, self._writer = database, OutboxWriter(clock)

    async def publish(self, message: EventEnvelope) -> None:
        async with self._database.transaction() as transaction:
            await self._writer.append(transaction, message)
