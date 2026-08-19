from pathlib import Path

from apps.quant_agent.cli import parser

RUNBOOK = Path(__file__).parents[1] / "docs/operations/mvp-runbook.md"


def test_i06_runbook_has_required_incidents_and_live_cli_commands() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for section in (
        "队列堆积", "SQLite 不可用或损坏", "Skill 或模型不可用", "费用超限",
        "重复通知或其他副作用", "脑区崩溃循环", "SAFE_MODE 解除", "演练记录与签字",
    ):
        assert any(line.startswith("## ") and section in line for line in text.splitlines())
    for argv in (
        ("--database", "facts.db", "health"),
        ("--database", "facts.db", "diagnose", "--limit", "20"),
        ("--database", "facts.db", "metrics"),
        ("--database", "facts.db", "metrics", "--prometheus"),
        ("--database", "facts.db", "replay", "correlation"),
    ):
        parser().parse_args(argv)
    assert "UPDATE task SET status" not in text
    assert "DELETE FROM outbox" not in text
    assert "不得用新 key" in text
    assert "tests/test_t02_transaction_fault_injection.py" in text
