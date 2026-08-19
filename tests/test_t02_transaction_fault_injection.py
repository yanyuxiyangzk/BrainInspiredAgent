from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction

STAMP = "2026-08-18T08:00:00Z"
CORR = "fault-correlation"


async def _seed(database: SQLiteDatabase) -> None:
    async with database.transaction() as tx:
        await tx.execute("INSERT INTO plan VALUES ('base-plan', '{}', 'd', 'CANDIDATE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO plan VALUES ('grant-plan', '{}', 'g', 'CANDIDATE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO plan_decision VALUES ('grant-decision', 'grant-plan', 'APPROVED', '{}', ?, ?)", (STAMP, CORR))
        await tx.execute("INSERT INTO plan VALUES ('task-plan', '{}', 't', 'CANDIDATE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO plan_decision VALUES ('task-decision', 'task-plan', 'APPROVED', '{}', ?, ?)", (STAMP, CORR))
        await tx.execute("INSERT INTO execution_grant VALUES ('task-grant', 'task-decision', 'reserved-task', '{}', 'ACTIVE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO plan VALUES ('episode-plan', '{}', 'e', 'CANDIDATE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO plan_decision VALUES ('episode-decision', 'episode-plan', 'APPROVED', '{}', ?, ?)", (STAMP, CORR))
        await tx.execute("INSERT INTO execution_grant VALUES ('episode-grant', 'episode-decision', 'episode-task', '{}', 'ACTIVE', ?, ?, ?)", (STAMP, STAMP, CORR))
        await tx.execute("INSERT INTO task(task_id, grant_id, status, created_at, deadline, correlation_id) VALUES ('episode-task', 'episode-grant', 'SUCCEEDED', ?, ?, ?)", (STAMP, STAMP, CORR))


async def _outbox(tx: SQLiteTransaction, event_id: str) -> None:
    await tx.execute(
        """INSERT INTO outbox_event(event_id, msg_type, envelope_json, publish_state,
                                    created_at, correlation_id)
           VALUES (?, ?, '{}', 'PENDING', ?, ?)""",
        (event_id, f"fault.{event_id}", STAMP, CORR),
    )


async def _t1(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO inbox_message(consumer_id,msg_id,status,received_at,correlation_id) VALUES ('c','fault-inbox','DONE',?,?)", (STAMP, CORR))


async def _t2(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO plan_decision VALUES ('fault-decision','base-plan','APPROVED','{}',?,?)", (STAMP, CORR))


async def _t3(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO execution_grant VALUES ('fault-grant','grant-decision','fault-grant-task','{}','ACTIVE',?,?,?)", (STAMP, STAMP, CORR))


async def _t4(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO task(task_id,grant_id,status,created_at,deadline,correlation_id) VALUES ('reserved-task','task-grant','PENDING',?,?,?)", (STAMP, STAMP, CORR))


async def _t5(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO artifact VALUES ('fault-artifact','local://fault','digest',1,'text/plain',?,?)", (STAMP, CORR))


async def _t6(tx: SQLiteTransaction) -> None:
    await tx.execute("INSERT INTO episode VALUES ('fault-episode','episode-task','{}',?,?)", (STAMP, CORR))


BOUNDARIES: tuple[tuple[str, Callable[[SQLiteTransaction], Awaitable[None]], str, str, str], ...] = (
    ("t1", _t1, "inbox_message", "msg_id", "fault-inbox"),
    ("t2", _t2, "plan_decision", "decision_id", "fault-decision"),
    ("t3", _t3, "execution_grant", "grant_id", "fault-grant"),
    ("t4", _t4, "task", "task_id", "reserved-task"),
    ("t5", _t5, "artifact", "artifact_id", "fault-artifact"),
    ("t6", _t6, "episode", "episode_id", "fault-episode"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "fact", "table", "column", "identity"), BOUNDARIES)
async def test_t1_to_t6_crash_before_commit_has_no_half_state(
    tmp_path: Path, name: str, fact: Callable[[SQLiteTransaction], Awaitable[None]],
    table: str, column: str, identity: str,
) -> None:
    database = SQLiteDatabase(tmp_path / f"{name}.db")
    await database.initialize()
    await _seed(database)
    with pytest.raises(RuntimeError, match="injected crash"):
        async with database.transaction() as tx:
            await fact(tx)
            await _outbox(tx, f"{name}-event")
            raise RuntimeError("injected crash before commit")
    assert await database.fetch_one(f"SELECT 1 FROM {table} WHERE {column} = ?", (identity,)) is None
    assert await database.fetch_one("SELECT 1 FROM outbox_event WHERE event_id = ?", (f"{name}-event",)) is None

    async with database.transaction() as tx:
        await fact(tx)
        await _outbox(tx, f"{name}-event")
    assert await database.fetch_one(f"SELECT 1 FROM {table} WHERE {column} = ?", (identity,)) is not None
    assert await database.fetch_one("SELECT 1 FROM outbox_event WHERE event_id = ?", (f"{name}-event",)) is not None
    await database.close()
