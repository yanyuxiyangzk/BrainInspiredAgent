"""Organization DNA delegation adapter for the governed execution entry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from active_agent_platform.coordinator import CognitiveCycle
from active_agent_platform.dna_execution import (
    DnaExecutionError,
    DnaExecutionIdentity,
    DnaIdentity,
)
from active_agent_platform.governed_execution import GovernedCognitiveApp, GovernedExecutionResult
from active_agent_platform.skills import SkillBinding
from active_agent_platform.state import BrainState
from domain_sdk.agent_dna import PersistentAgentDnaRegistry, WorkflowDnaReference
from domain_sdk.dna import DnaStatus
from domain_sdk.dna_repository import PersistentDnaRegistry
from domain_sdk.organization_dna import PersistentOrganizationDnaRegistry


@dataclass(frozen=True, slots=True)
class OrganizationExecutionRequest:
    organization_dna_id: str
    responsibility: str
    workflow_role: str
    cycle: CognitiveCycle
    state: BrainState
    bindings: Mapping[tuple[str, str, str], SkillBinding]
    requested_tokens: int
    requested_cost_minor: int
    requested_duration_seconds: int
    unavailable_roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.organization_dna_id or not self.responsibility or not self.workflow_role:
            raise DnaExecutionError("Organization execution request is incomplete")
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


@dataclass(frozen=True, slots=True)
class OrganizationExecutionResult:
    identity: DnaExecutionIdentity
    responsibility: str
    execution: GovernedExecutionResult


class OrganizationGovernedApp:
    """Freeze an Organization delegation and enter the existing governed runtime."""

    def __init__(self, governed: GovernedCognitiveApp,
                 organizations: PersistentOrganizationDnaRegistry,
                 agents: PersistentAgentDnaRegistry,
                 workflows: PersistentDnaRegistry) -> None:
        self._governed = governed
        self._organizations, self._agents, self._workflows = organizations, agents, workflows

    async def execute(self, request: OrganizationExecutionRequest) -> OrganizationExecutionResult:
        organization = (await self._organizations.active(request.organization_dna_id)).dna
        organization.approve_budget(
            tokens=request.requested_tokens, cost_minor=request.requested_cost_minor,
            duration_seconds=request.requested_duration_seconds, parallel_agents=1,
        )
        member = organization.delegate(
            request.responsibility, unavailable_roles=request.unavailable_roles,
        )
        agent = (await self._agents.get(member.agent_dna_id, member.agent_version)).dna
        if (agent.status is not DnaStatus.ACTIVE
                or agent.content_digest != member.agent_content_digest):
            raise DnaExecutionError("delegated Agent DNA is not active or its digest drifted")
        workflow_ref = _workflow_ref(agent.workflow_dna, request.workflow_role)
        workflow = (await self._workflows.get(workflow_ref.dna_id, workflow_ref.version)).dna
        if (workflow.status is not DnaStatus.ACTIVE
                or workflow.content_digest != workflow_ref.content_digest):
            raise DnaExecutionError("delegated Workflow DNA is not active or its digest drifted")
        identity = DnaExecutionIdentity(
            DnaIdentity(organization.dna_id, organization.version, organization.content_digest),
            member.role, DnaIdentity(agent.dna_id, agent.version, agent.content_digest),
            DnaIdentity(workflow.dna_id, workflow.version, workflow.content_digest),
        )
        result = await self._governed.execute(
            request.cycle, request.state, request.bindings,
            dna_identity=identity, responsibility=request.responsibility,
        )
        return OrganizationExecutionResult(identity, request.responsibility, result)


def _workflow_ref(refs: tuple[WorkflowDnaReference, ...], role: str) -> WorkflowDnaReference:
    matches = [item for item in refs if item.role == role]
    if len(matches) != 1:
        raise DnaExecutionError(f"Agent DNA has no unique Workflow role: {role}")
    return matches[0]
