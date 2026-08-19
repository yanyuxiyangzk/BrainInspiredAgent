import asyncio
import sqlite3
from pathlib import Path

import pytest

from active_agent_platform.storage import (
    DEFAULT_MIGRATIONS,
    Migration,
    MigrationError,
    SQLiteDatabase,
    SQLiteRepository,
)

EXPECTED_FACT_TABLES = {
    "inbox_message",
    "outbox_event",
    "dead_letter",
    "plan",
    "plan_decision",
    "execution_grant",
    "task",
    "task_transition",
    "workflow_run",
    "node_run",
    "skill_binding",
    "episode",
    "outcome_evaluation",
    "memory_entry",
    "workflow_definition",
    "skill_manifest",
    "capability_contract",
    "evolution_lineage",
    "artifact",
    "audit_record",
}


@pytest.mark.asyncio
async def test_initialize_enables_sqlite_safety_and_creates_fact_schema(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db", busy_timeout_ms=1234)
    await database.initialize()

    journal = await database.fetch_one("PRAGMA journal_mode")
    foreign_keys = await database.fetch_one("PRAGMA foreign_keys")
    timeout = await database.fetch_one("PRAGMA busy_timeout")
    tables = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )

    assert journal is not None and str(journal[0]).lower() == "wal"
    assert foreign_keys is not None and foreign_keys[0] == 1
    assert timeout is not None and timeout[0] == 1234
    names = {str(row["name"]) for row in tables}
    assert EXPECTED_FACT_TABLES <= names
    assert "schema_migration" in names
    await database.close()


@pytest.mark.asyncio
async def test_migrations_are_idempotent_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    first = SQLiteDatabase(path)
    await first.initialize()
    applied = await first.fetch_all("SELECT version, checksum FROM schema_migration")
    await first.close()

    second = SQLiteDatabase(path)
    await second.initialize()
    reapplied = await second.fetch_all("SELECT version, checksum FROM schema_migration")
    await second.close()

    assert [tuple(row) for row in applied] == [tuple(row) for row in reapplied]
    assert applied[0]["checksum"] == DEFAULT_MIGRATIONS[0].checksum


@pytest.mark.asyncio
async def test_unknown_applied_migration_stops_startup(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    await database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migration(version, checksum) VALUES (?, ?)",
            ("999_unknown", "digest"),
        )

    with pytest.raises(MigrationError, match="unknown applied migration"):
        await SQLiteDatabase(path).initialize()


@pytest.mark.asyncio
async def test_modified_migration_checksum_stops_startup(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    database = SQLiteDatabase(path)
    await database.initialize()
    await database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE schema_migration SET checksum = 'changed' WHERE version = ?",
            (DEFAULT_MIGRATIONS[0].version,),
        )

    with pytest.raises(MigrationError, match="checksum mismatch"):
        await SQLiteDatabase(path).initialize()


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_all_its_statements(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    broken = Migration(
        "001_broken",
        (
            "CREATE TABLE should_rollback(id TEXT PRIMARY KEY)",
            "INSERT INTO table_that_does_not_exist(id) VALUES ('x')",
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        await SQLiteDatabase(path, migrations=(broken,)).initialize()

    with sqlite3.connect(path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'should_rollback'"
        ).fetchone()
    assert table is None


@pytest.mark.asyncio
async def test_transaction_commit_and_rollback_are_atomic(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()

    async with database.transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO inbox_message(
                consumer_id, msg_id, status, received_at, correlation_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("consumer", "committed", "DONE", "2026-08-17T00:00:00Z", "correlation"),
        )
    with pytest.raises(RuntimeError, match="force rollback"):
        async with database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO inbox_message(
                    consumer_id, msg_id, status, received_at, correlation_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("consumer", "rolled-back", "DONE", "2026-08-17T00:00:00Z", "correlation"),
            )
            raise RuntimeError("force rollback")

    rows = await database.fetch_all("SELECT msg_id FROM inbox_message ORDER BY msg_id")
    assert [row["msg_id"] for row in rows] == ["committed"]
    await database.close()


@pytest.mark.asyncio
async def test_repository_uses_caller_transaction_and_batch_operations(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    async with database.transaction() as transaction:
        repository = SQLiteRepository(transaction)
        await transaction.executemany(
            """
            INSERT INTO artifact(
                artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ("a1", "local://a1", "d1", 1, "text/plain", "now", "c1"),
                ("a2", "local://a2", "d2", 2, "text/plain", "now", "c1"),
            ),
        )
        one = await repository.fetch_one("SELECT * FROM artifact WHERE artifact_id = ?", ("a1",))
        all_rows = await repository.fetch_all("SELECT * FROM artifact ORDER BY artifact_id")
        await repository.execute("UPDATE artifact SET size_bytes = ? WHERE artifact_id = ?", (3, "a1"))

    assert one is not None and one["digest"] == "d1"
    assert [row["artifact_id"] for row in all_rows] == ["a1", "a2"]
    updated = await database.fetch_one("SELECT size_bytes FROM artifact WHERE artifact_id = 'a1'")
    assert updated is not None and updated[0] == 3
    await database.close()


@pytest.mark.asyncio
async def test_constraints_prevent_duplicate_inbox_and_orphan_decision(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    async with database.transaction() as transaction:
        await transaction.execute(
            """
            INSERT INTO inbox_message(
                consumer_id, msg_id, status, received_at, correlation_id
            ) VALUES ('consumer', 'message', 'DONE', 'now', 'correlation')
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO inbox_message(
                    consumer_id, msg_id, status, received_at, correlation_id
                ) VALUES ('consumer', 'message', 'DONE', 'now', 'correlation')
                """
            )
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as transaction:
            await transaction.execute(
                """
                INSERT INTO plan_decision(
                    decision_id, plan_id, decision, decision_json, decided_at, correlation_id
                ) VALUES ('decision', 'missing', 'APPROVED', '{}', 'now', 'correlation')
                """
            )
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_transactions_are_serialized(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with database.transaction():
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with database.transaction():
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-enter", "first-exit", "second-enter"]
    await database.close()


@pytest.mark.asyncio
async def test_database_lifecycle_and_configuration_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="busy_timeout"):
        SQLiteDatabase(tmp_path / "bad.db", busy_timeout_ms=0)
    with pytest.raises(ValueError, match="unique and sorted"):
        SQLiteDatabase(
            tmp_path / "bad.db",
            migrations=(Migration("002", ("SELECT 1",)), Migration("001", ("SELECT 1",))),
        )

    database = SQLiteDatabase(tmp_path / "facts.db")
    with pytest.raises(RuntimeError, match="not initialized"):
        await database.fetch_one("SELECT 1")
    await database.close()
    await database.initialize()
    with pytest.raises(RuntimeError, match="already initialized"):
        await database.initialize()
    await database.close()
    await database.close()
