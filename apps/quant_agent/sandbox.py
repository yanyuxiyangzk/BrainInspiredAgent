"""Quant skill layer for the DNA sandbox executor (E01).

Binds the deterministic fake market skills (D07) to a sandbox virtual
clock, so replay measurements never touch real market data or production
fact tables.
"""

from __future__ import annotations

from active_agent_platform.foundation import FakeClock
from active_agent_platform.skills import CapabilityRegistry, SkillRegistry
from apps.quant_agent.fake_skills import install_fake_skills
from domain_sdk.dna_sandbox_executor import (
    SandboxPolicy,
    SandboxSkillLayer,
    WorkflowSandboxExecutor,
)

SANDBOX_PERMISSIONS = frozenset({"market.read", "notification.local.write"})


def quant_fake_skill_layer(clock: FakeClock) -> SandboxSkillLayer:
    """Build an isolated fake market skill stack for one sandbox execution."""
    capabilities = CapabilityRegistry()
    skills = SkillRegistry(capabilities)
    bundle = install_fake_skills(capabilities, skills, clock=clock)
    return SandboxSkillLayer(capabilities, skills, bundle.adapters)


def quant_sandbox_executor() -> WorkflowSandboxExecutor:
    """Sandbox executor wired with the quant fake skill stack."""
    return WorkflowSandboxExecutor(
        quant_fake_skill_layer,
        policy=SandboxPolicy(permissions=SANDBOX_PERMISSIONS),
    )
