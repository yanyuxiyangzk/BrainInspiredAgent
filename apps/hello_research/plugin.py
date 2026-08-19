"""A tiny research-text plugin assembled entirely through Domain SDK contracts."""

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
from domain_sdk.contracts import JsonValue


class UppercaseSkill:
    async def invoke(self, input_data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return {"text": str(input_data["text"]).upper(), "style": "upper"}


class LowercaseSkill:
    async def invoke(self, input_data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return {"text": str(input_data["text"]).lower(), "style": "lower"}


class TextOutcomeEvaluator:
    async def evaluate(self, output: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        text = str(output.get("text", ""))
        return {"accepted": bool(text.strip()), "length": len(text)}


class HelloResearchPlugin:
    def contribute(self) -> PluginContribution:
        capability = CapabilityContract(
            capability="research.text.normalize",
            version="1.0",
            input_schema={"type": "object", "required": ["text"]},
            output_schema={"type": "object", "required": ["text", "style"]},
            side_effect=SideEffect.PURE,
        )
        skills = (
            SkillRegistration(
                SkillManifest(
                    skill_id="text-upper",
                    version="1.0.0",
                    digest="sha256:hello-upper-v1",
                    capabilities=(capability.capability,),
                ),
                UppercaseSkill(),
            ),
            SkillRegistration(
                SkillManifest(
                    skill_id="text-lower",
                    version="1.0.0",
                    digest="sha256:hello-lower-v1",
                    capabilities=(capability.capability,),
                ),
                LowercaseSkill(),
            ),
        )
        workflow = WorkflowRegistration(
            workflow_id="normalize_research_note",
            version="1.0.0",
            definition={
                "workflow_id": "normalize_research_note",
                "version": "1.0.0",
                "nodes": [
                    {
                        "node_id": "normalize",
                        "type": "skill",
                        "capability": capability.capability,
                    }
                ],
            },
            required_capabilities=(capability.capability,),
        )
        profile = LoopProfile(
            profile_id="research-note-review",
            version="1.0.0",
            workflow_id=workflow.workflow_id,
            trigger="manual-or-scheduled",
        )
        return PluginContribution(
            plugin_id="hello_research",
            capabilities=(capability,),
            skills=skills,
            workflows=(workflow,),
            loop_profiles=(profile,),
            evaluators=(TextOutcomeEvaluator(),),
        )
