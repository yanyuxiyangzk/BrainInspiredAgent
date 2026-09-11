"""因子发现循环自动运行入口测试：bia factor-loop run/status。

把 L-001～L-009 的库能力装配成可从 CLI 反复调用的自动运行入口：每轮
生成→审查→越界过滤→FSA→回测→反馈→事务提交，checkpoint 跨调用续跑。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from active_agent_platform.storage import SQLiteDatabase
from apps.quant_agent.factor_loop_app import factor_loop_status, run_factor_rounds


@pytest.mark.asyncio
async def test_factor_loop_rounds_run_and_persist(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    checkpoint = tmp_path / "pointer.json"
    summary = await run_factor_rounds(
        database, checkpoint, rounds=6, seed=20260907,
    )
    assert summary["rounds"] == 6
    assert summary["candidates"] > 0
    assert summary["backtests"] > 0
    assert summary["iteration"] == 6
    assert summary["status"] == "RUNNING"
    assert summary["completed_events"] == 6  # hooks 摘要按轮累计


@pytest.mark.asyncio
async def test_factor_loop_resumes_across_invocations(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    checkpoint = tmp_path / "pointer.json"
    first = await run_factor_rounds(database, checkpoint, rounds=4, seed=11)
    second = await run_factor_rounds(database, checkpoint, rounds=3, seed=11)
    assert first["iteration"] == 4
    assert second["iteration"] == 7  # 续跑而不是重置
    assert second["completed_events"] == 3  # 新调用只统计新轮次
    pointer_state = await factor_loop_status(database, checkpoint)
    assert pointer_state["iteration"] == 7
    assert pointer_state["search_state"] is not None  # L-004 状态已入指针


@pytest.mark.asyncio
async def test_factor_loop_status_before_first_run(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    state = await factor_loop_status(database, tmp_path / "pointer.json")
    assert state["status"] == "UNINITIALIZED"


@pytest.mark.asyncio
async def test_factor_loop_rejects_non_positive_rounds(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "factor.db")
    await database.initialize()
    with pytest.raises(ValueError, match="rounds"):
        await run_factor_rounds(database, tmp_path / "pointer.json", rounds=0, seed=1)
