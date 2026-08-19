"""Async-safe SQLite connection, transactions and migration runner."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from active_agent_platform.storage.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    MigrationError,
)

SQLiteParameters = Sequence[str | int | float | bytes | None]


class SQLiteTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def execute(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> sqlite3.Cursor:
        return await asyncio.to_thread(self._connection.execute, statement, parameters)

    async def executemany(
        self, statement: str, parameters: Iterable[SQLiteParameters]
    ) -> sqlite3.Cursor:
        values = tuple(parameters)
        return await asyncio.to_thread(self._connection.executemany, statement, values)

    async def fetch_one(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> sqlite3.Row | None:
        cursor = await self.execute(statement, parameters)
        return await asyncio.to_thread(cursor.fetchone)

    async def fetch_all(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> list[sqlite3.Row]:
        cursor = await self.execute(statement, parameters)
        rows = await asyncio.to_thread(cursor.fetchall)
        return list(rows)


class SQLiteDatabase:
    def __init__(
        self,
        path: str | Path,
        *,
        migrations: tuple[Migration, ...] = DEFAULT_MIGRATIONS,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        versions = [migration.version for migration in migrations]
        if versions != sorted(versions) or len(versions) != len(set(versions)):
            raise ValueError("migration versions must be unique and sorted")
        self._path = str(path)
        self._migrations = migrations
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._connection is not None:
                raise RuntimeError("database is already initialized")
            connection = await asyncio.to_thread(self._connect)
            try:
                await asyncio.to_thread(self._configure, connection)
                await self._run_migrations(connection)
            except BaseException:
                await asyncio.to_thread(connection.close)
                raise
            self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            if self._connection is None:
                return
            connection, self._connection = self._connection, None
            await asyncio.to_thread(connection.close)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SQLiteTransaction]:
        async with self._lock:
            connection = self._require_connection()
            begin = asyncio.create_task(asyncio.to_thread(connection.execute, "BEGIN IMMEDIATE"))
            try:
                await asyncio.shield(begin)
            except asyncio.CancelledError:
                await begin
                await asyncio.to_thread(connection.rollback)
                raise
            transaction = SQLiteTransaction(connection)
            try:
                yield transaction
            except BaseException:
                await _finish(connection.rollback)
                raise
            else:
                await _finish(connection.commit)

    async def fetch_one(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> sqlite3.Row | None:
        async with self._lock:
            connection = self._require_connection()
            cursor = await asyncio.to_thread(connection.execute, statement, parameters)
            return await asyncio.to_thread(cursor.fetchone)

    async def fetch_all(
        self, statement: str, parameters: SQLiteParameters = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            connection = self._require_connection()
            cursor = await asyncio.to_thread(connection.execute, statement, parameters)
            return list(await asyncio.to_thread(cursor.fetchall))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _configure(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

    async def _run_migrations(self, connection: sqlite3.Connection) -> None:
        await asyncio.to_thread(
            connection.execute,
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
        )
        rows = await asyncio.to_thread(
            lambda: connection.execute(
                "SELECT version, checksum FROM schema_migration ORDER BY version"
            ).fetchall()
        )
        known = {migration.version: migration for migration in self._migrations}
        for row in rows:
            version = str(row["version"])
            if version not in known:
                raise MigrationError(f"unknown applied migration: {version}")
            if row["checksum"] != known[version].checksum:
                raise MigrationError(f"migration checksum mismatch: {version}")
        applied = {str(row["version"]) for row in rows}
        for migration in self._migrations:
            if migration.version not in applied:
                await self._apply_migration(connection, migration)

    async def _apply_migration(
        self, connection: sqlite3.Connection, migration: Migration
    ) -> None:
        try:
            await asyncio.to_thread(connection.execute, "BEGIN IMMEDIATE")
            for statement in migration.statements:
                await asyncio.to_thread(connection.execute, statement)
            await asyncio.to_thread(
                connection.execute,
                "INSERT INTO schema_migration(version, checksum) VALUES (?, ?)",
                (migration.version, migration.checksum),
            )
            await asyncio.to_thread(connection.commit)
        except BaseException:
            await asyncio.to_thread(connection.rollback)
            raise

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not initialized")
        return self._connection


async def _finish(operation: Callable[[], None]) -> None:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
