from io import StringIO

import pytest

from apps.quant_agent.cli import run


@pytest.mark.asyncio
async def test_evolution_governance_is_safe_by_default(tmp_path):
    database = tmp_path / "release.db"
    for view in ("promote", "rollback", "kill"):
        out, err = StringIO(), StringIO()
        code = await run(("--database", str(database), "evolution", view), out, err)
        assert code == 0
        assert '"status":"REJECTED"' in out.getvalue()


@pytest.mark.asyncio
async def test_dna_transition_requires_explicit_governance_fields(tmp_path):
    out, err = StringIO(), StringIO()
    code = await run(("--database", str(tmp_path / "release.db"), "dna", "transition", "missing"), out, err)
    assert code == 2
    assert "DNA transition requires" in err.getvalue()
