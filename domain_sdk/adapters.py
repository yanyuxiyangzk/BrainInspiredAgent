"""Adapters bridging public domain contracts to the platform invocation protocol."""

from typing import cast

from active_agent_platform.skills import SkillContext, SkillError, SkillInvocation, SkillResult
from domain_sdk.contracts import JsonValue, SkillAdapter


class DomainSkillBridge:
    def __init__(self, adapter: SkillAdapter) -> None:
        self._adapter = adapter

    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult:
        if context.cancellation.cancelled:
            raise SkillError("CANCELLED", "domain skill invocation was cancelled")
        output = await self._adapter.invoke(cast("dict[str, JsonValue]", invocation.input))
        if context.cancellation.cancelled:
            raise SkillError("CANCELLED", "domain skill invocation was cancelled")
        return SkillResult("SUCCEEDED", output)
