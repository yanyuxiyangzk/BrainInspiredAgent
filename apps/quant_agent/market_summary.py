"""Application-owned proactive market summary Workflow definition."""

from typing import Final

from apps.quant_agent.fake_skills import (
    MARKET_CAPABILITY,
    NOTIFICATION_CAPABILITY,
    SUMMARY_CAPABILITY,
)

MARKET_SUMMARY_WORKFLOW: Final[dict[str, object]] = {
    "spec_version": "1.0",
    "workflow_id": "market_summary",
    "version": "1.0.0",
    "name": "Market summary",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "symbols": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "trade_date": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["symbols", "trade_date", "title"],
    },
    "policy": {
        "timeout_seconds": 10,
        "max_parallelism": 1,
        "required_capabilities": [
            MARKET_CAPABILITY,
            SUMMARY_CAPABILITY,
            NOTIFICATION_CAPABILITY,
        ],
    },
    "nodes": [
        {
            "node_id": "read_snapshot",
            "type": "skill",
            "depends_on": [],
            "capability": MARKET_CAPABILITY,
            "capability_version": "1.0",
            "input": {
                "symbols": "$.params.symbols",
                "trade_date": "$.params.trade_date",
            },
            "constraints": {"side_effect": "PURE"},
        },
        {
            "node_id": "build_summary",
            "type": "skill",
            "depends_on": ["read_snapshot"],
            "capability": SUMMARY_CAPABILITY,
            "capability_version": "1.0",
            "input": {
                "title": "$.params.title",
                "items": "$.nodes.read_snapshot.output.quotes",
                "max_items": 10,
            },
            "constraints": {"side_effect": "PURE"},
        },
        {
            "node_id": "notify",
            "type": "skill",
            "depends_on": ["build_summary"],
            "capability": NOTIFICATION_CAPABILITY,
            "capability_version": "1.0",
            "input": {
                "title": "$.params.title",
                "message": "$.nodes.build_summary.output.summary",
                "level": "INFO",
            },
            "constraints": {"side_effect": "IDEMPOTENT"},
        },
    ],
    "output_mapping": {
        "summary": "$.nodes.build_summary.output.summary",
        "item_count": "$.nodes.build_summary.output.item_count",
        "notification_id": "$.nodes.notify.output.notification_id",
        "delivered": "$.nodes.notify.output.delivered",
    },
}
