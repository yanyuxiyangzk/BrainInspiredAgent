"""Canonical command descriptions shared by the CLI and interactive shell."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    summary: str
    usage: str
    argv: tuple[str, ...] | None = None
    aliases: tuple[str, ...] = ()


COMMAND_SPECS = (
    CommandSpec("/system", "inspect operational system facts", "/system [status|health|diagnose|metrics|logs|migrations]"),
    CommandSpec("/brain", "show derived cognitive state", "/brain [state|areas|cycles]"),
    CommandSpec("/events", "inspect event delivery facts", "/events [recent|show|correlation|inbox|dead-letter]"),
    CommandSpec("/plans", "inspect plans and decisions", "/plans [recent|show|rejected]"),
    CommandSpec("/tasks", "inspect or control durable tasks", "/tasks [list|running|failed|show|trace|cancel|retry]"),
    CommandSpec("/catalog", "list capabilities, skills and workflows", "/catalog [capabilities|skills|workflows]"),
    CommandSpec("/skills", "inspect registered skills", "/skills [list|show|health|bindings]"),
    CommandSpec("/workflows", "inspect registered workflows and runs", "/workflows [list|active|show|runs]"),
    CommandSpec("/dna", "inspect or govern DNA definitions", "/dna [list|active|show|lineage|explain|executions|transition]"),
    CommandSpec("/evolution", "inspect DNA evolution evidence", "/evolution [candidates|fitness|datasets|replay|compare|campaigns|explain|promote|rollback|kill]"),
    CommandSpec("/market", "submit a governed market summary", "/market SYMBOLS [--title TEXT]"),
    CommandSpec("/commands", "list or inspect command receipts", "/commands [MESSAGE_ID]", ("commands",)),
    CommandSpec("/insights", "query and explain market insights", "/insights [latest|show|explain]", ("insights", "latest")),
    CommandSpec("/subscribe", "add a local subscription", "/subscribe [USER]"),
    CommandSpec("/subscriptions", "inspect subscription preferences", "/subscriptions [list|enable|disable]"),
    CommandSpec("/deliveries", "list local deliveries", "/deliveries [USER]"),
    CommandSpec("/health", "check database liveness and readiness", "/health", ("health",)),
    CommandSpec("/status", "show durable platform status", "/status", ("status",)),
    CommandSpec("/diagnose", "show a diagnostic snapshot", "/diagnose", ("diagnose",)),
    CommandSpec("/loop", "inspect the active LoopEngine supervisor", "/loop [status|services|lag|checkpoints]"),
    CommandSpec("/trace", "replay a governed correlation trace", "/trace CORRELATION_ID"),
    CommandSpec("/help", "show commands or detailed command help", "/help [COMMAND]"),
    CommandSpec("/exit", "stop cleanly and leave", "/exit", aliases=("/quit",)),
)


def command_help(name: str | None = None) -> str:
    if name is not None:
        canonical = name if name.startswith("/") else f"/{name}"
        spec = next((item for item in COMMAND_SPECS
                     if canonical == item.name or canonical in item.aliases), None)
        if spec is None:
            raise KeyError(canonical)
        return f"{spec.usage}\n  {spec.summary}\n"
    lines = ["Available commands:"]
    lines.extend(f"  {item.usage:<38} {item.summary}" for item in COMMAND_SPECS)
    return "\n".join(lines) + "\n"


COMMANDS = tuple(item.name for item in COMMAND_SPECS)
