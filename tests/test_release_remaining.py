from io import StringIO

import pytest

from apps.quant_agent.cli import run


@pytest.mark.asyncio
async def test_evolution_compare_requires_two_ids(tmp_path):
    out, err = StringIO(), StringIO()
    code = await run(("--database", str(tmp_path / "x.db"), "evolution", "compare"), out, err)
    assert code == 0
    assert "requires two" in out.getvalue()


@pytest.mark.asyncio
async def test_high_risk_evolution_commands_remain_governed(tmp_path):
    for view in ("promote", "rollback", "kill"):
        out, err = StringIO(), StringIO()
        code = await run(("--database", str(tmp_path / f"{view}.db"), "evolution", view), out, err)
        assert code == 0
        assert "REJECTED" in out.getvalue()
