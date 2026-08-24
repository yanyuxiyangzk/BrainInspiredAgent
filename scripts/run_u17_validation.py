"""Run the U17 command-surface black-box and recovery release gate."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from run_q08_validation import _cli, validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commands", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    report = command_surface_validate(args.database, args.commands, args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


def command_surface_validate(database: Path, commands: int, timeout: float) -> dict[str, object]:
    report = validate(database, commands, timeout)
    errors = list(report["errors"])
    before = _business_facts(database)
    queries = (
        ("system", "health"), ("brain", "state"), ("events", "recent"),
        ("plans", "recent"), ("tasks", "list"), ("catalog", "capabilities"),
        ("skills", "list"), ("workflows", "active"), ("dna", "active"),
        ("dna", "executions"), ("insights", "latest"),
    )
    query_failures: list[str] = []
    for arguments in queries:
        try:
            _cli(database, *arguments)
        except Exception as error:  # noqa: BLE001 - preserve black-box evidence
            query_failures.append(f"{' '.join(arguments)}: {error}")
    after = _business_facts(database)
    if before != after:
        errors.append(f"read-only queries changed business facts: {before} -> {after}")
    if query_failures:
        errors.extend(query_failures)
    rejected = [_cli(database, "evolution", action).get("status")
                for action in ("promote", "rollback", "kill")]
    if rejected != ["REJECTED"] * 3:
        errors.append(f"unsafe evolution governance result: {rejected}")
    contexts = _dna_contexts(database)
    if contexts != commands:
        errors.append(f"DNA execution contexts: expected {commands}, found {contexts}")
    report.update({
        "gate": "U17", "status": "PASSED" if not errors else "FAILED",
        "release_decision": "RELEASABLE" if not errors else "BLOCKED",
        "query_commands": len(queries), "query_failures": query_failures,
        "query_business_facts_unchanged": before == after,
        "governance_rejections": rejected, "dna_execution_contexts": contexts,
        "errors": errors,
    })
    return report


def _business_facts(database: Path) -> dict[str, int]:
    tables = ("plan", "task", "workflow_run", "episode", "outcome_evaluation", "outbox_event")
    with sqlite3.connect(database) as connection:
        return {table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in tables}


def _dna_contexts(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(connection.execute("SELECT count(*) FROM dna_execution_context").fetchone()[0])


if __name__ == "__main__":
    raise SystemExit(main())
