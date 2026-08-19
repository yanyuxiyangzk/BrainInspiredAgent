"""Base class for repositories participating in an existing transaction."""

import sqlite3

from active_agent_platform.storage.database import SQLiteParameters, SQLiteTransaction


class SQLiteRepository:
    def __init__(self, transaction: SQLiteTransaction) -> None:
        self._transaction = transaction

    async def execute(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> sqlite3.Cursor:
        return await self._transaction.execute(statement, parameters)

    async def fetch_one(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> sqlite3.Row | None:
        return await self._transaction.fetch_one(statement, parameters)

    async def fetch_all(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> list[sqlite3.Row]:
        return await self._transaction.fetch_all(statement, parameters)
