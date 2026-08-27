"""Governed runtime services around the provider-neutral LLM contracts."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from collections.abc import Mapping
from .llm import ChatMessage, ChatModel, ModelRequest, ModelResponse, LlmError, LlmErrorCode

@dataclass(frozen=True, slots=True)
class LlmConfig:
    provider: str
    model: str
    api_key_ref: str
    max_retries: int = 2
    timeout_seconds: float = 30.0
    daily_token_budget: int = 100_000
    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.api_key_ref or self.max_retries < 0 or self.timeout_seconds <= 0 or self.daily_token_budget < 1:
            raise ValueError("invalid LLM configuration")

@dataclass(slots=True)
class LlmUsage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    def record(self, response: ModelResponse) -> None:
        self.requests += 1; self.input_tokens += response.input_tokens; self.output_tokens += response.output_tokens

class LlmBudget:
    def __init__(self, token_limit: int) -> None:
        if token_limit < 1: raise ValueError("token limit must be positive")
        self.limit, self.used = token_limit, 0
    def reserve(self, tokens: int) -> None:
        if tokens < 0 or self.used + tokens > self.limit: raise LlmError(LlmErrorCode.RATE_LIMITED, "LLM token budget exceeded")
        self.used += tokens

class GovernedLlmClient:
    def __init__(self, model: ChatModel, config: LlmConfig, *, budget: LlmBudget | None = None) -> None:
        self.model, self.config, self.budget = model, config, budget or LlmBudget(config.daily_token_budget)
        self.usage = LlmUsage()
    async def generate(self, request: ModelRequest) -> ModelResponse:
        last: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                self.budget.reserve(request.timeout_seconds and 1)
                response = await asyncio.wait_for(self.model.generate(request), self.config.timeout_seconds)
                self.usage.record(response); return response
            except asyncio.CancelledError: raise
            except LlmError as exc:
                last = exc
                if exc.code not in {LlmErrorCode.UNAVAILABLE, LlmErrorCode.RATE_LIMITED, LlmErrorCode.TIMEOUT} or attempt >= self.config.max_retries: raise
                await asyncio.sleep(min(2 ** attempt, 8))
        raise LlmError(LlmErrorCode.UNAVAILABLE, str(last))

@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    messages: tuple[ChatMessage, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    def append(self, *messages: ChatMessage) -> "Conversation":
        return Conversation(self.conversation_id, self.messages + tuple(messages), self.metadata)

class ConversationService:
    def __init__(self, client: GovernedLlmClient) -> None: self.client, self._sessions = client, {}
    def get(self, conversation_id: str) -> Conversation: return self._sessions.setdefault(conversation_id, Conversation(conversation_id))
    async def send(self, conversation_id: str, content: str, *, correlation_id: str, system: str | None = None) -> ModelResponse:
        session = self.get(conversation_id)
        messages = session.messages + ((ChatMessage("system", system),) if system and not session.messages else ()) + (ChatMessage("user", content),)
        request = ModelRequest(messages, self.client.config.provider, self.client.config.model, correlation_id, timeout_seconds=self.client.config.timeout_seconds)
        response = await self.client.generate(request)
        self._sessions[conversation_id] = Conversation(conversation_id, messages + (ChatMessage("assistant", response.content),), session.metadata)
        return response
