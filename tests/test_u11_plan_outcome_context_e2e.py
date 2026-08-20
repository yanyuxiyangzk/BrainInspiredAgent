import pytest

from apps.quant_agent.runtime import build_quant_runtime


@pytest.mark.asyncio
async def test_runtime_exposes_complete_plan_outcome_context_contract(tmp_path):
    components = build_quant_runtime(tmp_path / "context.db")
    await components.database.initialize()
    await components.service.start()
    columns = await components.database.fetch_all("PRAGMA table_info(dna_execution_context)")
    names = {str(row["name"]) for row in columns}
    assert {
        "context_digest", "correlation_id", "plan_id", "decision_id", "grant_id",
        "task_id", "run_id", "episode_id", "evaluation_id", "organization_dna_id",
        "agent_dna_id", "workflow_dna_id", "organization_role",
    } <= names
    for table, key in (("plan", "plan_id"), ("plan_decision", "decision_id"),
                       ("execution_grant", "grant_id"), ("task", "task_id"),
                       ("workflow_run", "run_id"), ("episode", "episode_id"),
                       ("outcome_evaluation", "evaluation_id")):
        assert await components.database.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    await components.service.stop()
    await components.database.close()
