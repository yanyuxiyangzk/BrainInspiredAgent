"""Application-owned daily review Workflow definition."""

from typing import Final

from apps.quant_agent.fake_skills import SUMMARY_CAPABILITY

DAILY_REVIEW_WORKFLOW: Final[dict[str, object]] = {
    "spec_version": "1.0",
    "workflow_id": "daily_review",
    "version": "1.0.0",
    "name": "Daily review",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "review_key": {"type": "string"},
            "summary": {"type": "object"},
        },
        "required": ["review_key", "summary"],
    },
    "policy": {
        "timeout_seconds": 60,
        "max_parallelism": 1,
        "required_capabilities": [SUMMARY_CAPABILITY],
    },
    "nodes": [{
        "node_id": "summarize",
        "type": "skill",
        "depends_on": [],
        "capability": SUMMARY_CAPABILITY,
        "capability_version": "1.0",
        "input": {
            "title": "Daily review",
            "items": "$.params.summary.episode_ids",
        },
        "constraints": {"side_effect": "PURE"},
    }],
    "output_mapping": {"review": "$.nodes.summarize.output"},
}
