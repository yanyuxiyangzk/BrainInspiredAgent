import pytest

from apps.quant_agent.runtime import build_quant_runtime


@pytest.mark.asyncio
async def test_runtime_bootstraps_active_three_layer_dna(tmp_path):
    components = build_quant_runtime(tmp_path / "dna.db")
    await components.database.initialize()
    await components.service.start()
    for table, dna_id in (("dna_definition", "workflow.market_summary"),
                          ("dna_definition", "workflow.daily_review"),
                          ("agent_dna_definition", "agent.quant.default"),
                          ("organization_dna_definition", "org.quant.default")):
        row = await components.database.fetch_one(
            f"SELECT status FROM {table} WHERE dna_id=?", (dna_id,))
        assert row is not None and str(row["status"]) == "ACTIVE"
    await components.service.stop()
    await components.database.close()
