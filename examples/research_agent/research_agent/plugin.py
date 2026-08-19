"""A non-financial domain plugin built exclusively with public SDK contracts."""

from collections.abc import Mapping

from domain_sdk import (
    CapabilityContract,
    LoopProfile,
    PluginContribution,
    SideEffect,
    SkillManifest,
    SkillRegistration,
    WorkflowRegistration,
)

RESEARCH_CAPABILITY = "research.text.keywords"
RESEARCH_WORKFLOW: dict[str, object] = {
    "spec_version": "1.0",
    "workflow_id": "research_note_analysis",
    "version": "1.0.0",
    "name": "Research note analysis",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "policy": {
        "timeout_seconds": 10,
        "max_parallelism": 1,
        "required_capabilities": [RESEARCH_CAPABILITY],
    },
    "nodes": [{
        "node_id": "keywords",
        "type": "skill",
        "depends_on": [],
        "capability": RESEARCH_CAPABILITY,
        "capability_version": "1.0",
        "input": {"text": "$.params.text"},
        "constraints": {"side_effect": "PURE"},
    }],
    "output_mapping": {"keywords": "$.nodes.keywords.output.keywords"},
}


class ExtractKeywords:
    async def invoke(self, input_data: Mapping[str, object]) -> Mapping[str, object]:
        words = str(input_data["text"]).lower().split()
        return {"keywords": sorted(set(words))[:10]}


class ResearchAgentPlugin:
    def contribute(self) -> PluginContribution:
        capability = CapabilityContract(
            capability=RESEARCH_CAPABILITY,
            version="1.0",
            input_schema={"type": "object", "required": ["text"]},
            output_schema={"type": "object", "required": ["keywords"]},
            side_effect=SideEffect.PURE,
        )
        workflow = WorkflowRegistration(
            workflow_id="research_note_analysis",
            version="1.0.0",
            definition=RESEARCH_WORKFLOW,
            required_capabilities=(capability.capability,),
        )
        return PluginContribution(
            plugin_id="research_agent",
            capabilities=(capability,),
            skills=(SkillRegistration(
                SkillManifest(
                    skill_id="research-keywords",
                    version="1.0.0",
                    digest="sha256:research-keywords-v1",
                    capabilities=(capability.capability,),
                ),
                ExtractKeywords(),
            ),),
            workflows=(workflow,),
            loop_profiles=(LoopProfile(
                profile_id="research-note-analysis",
                version="1.0.0",
                workflow_id=workflow.workflow_id,
                trigger="manual",
            ),),
        )
