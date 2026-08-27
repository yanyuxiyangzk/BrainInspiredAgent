"""Domain-neutral contracts for governed language-model calls."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class LlmErrorCode(StrEnum):
    UNAVAILABLE = "MODEL_UNAVAILABLE"
    TIMEOUT = "MODEL_TIMEOUT"
    RATE_LIMITED = "MODEL_RATE_LIMITED"
    AUTHENTICATION = "MODEL_AUTHENTICATION"
    INVALID_OUTPUT = "MODEL_INVALID_OUTPUT"


class LlmError(RuntimeError):
    def __init__(self, code: LlmErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"} or not self.content:
            raise ValueError("invalid chat message")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ChatMessage, ...]
    provider: str
    model: str
    correlation_id: str
    temperature: float = 0.0
    seed: int | None = None
    timeout_seconds: float = 30.0
    response_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.messages or not self.provider or not self.model or not self.correlation_id:
            raise ValueError("model request identity and messages are required")
        if not 0 <= self.temperature <= 2 or self.timeout_seconds <= 0:
            raise ValueError("model request bounds are invalid")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    provider: str
    finish_reason: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    chat: bool = True
    structured_output: bool = False
    tool_calling: bool = False
    context_tokens: int = 0


class ChatModel(Protocol):
    capabilities: ModelCapabilities
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class StructuredChatModel(ChatModel, Protocol):
    async def generate_structured(self, request: ModelRequest) -> Mapping[str, object]: ...


class FakeChatModel:
    """Deterministic model for tests; never performs network I/O."""
    capabilities = ModelCapabilities(structured_output=True)

    def __init__(self, responses: Sequence[str | Mapping[str, object] | Exception], *, provider: str = "fake", model: str = "fake-1") -> None:
        self._responses = iter(responses)
        self.provider, self.model, self.requests = provider, model, []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        try: value = next(self._responses)
        except StopIteration as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "no fake response") from exc
        if isinstance(value, Exception): raise value
        return ModelResponse(value if isinstance(value, str) else str(value), self.model, self.provider, "stop")

    async def generate_structured(self, request: ModelRequest) -> Mapping[str, object]:
        response = await self.generate(request)
        if not isinstance(response.content, str): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output is not text")
        import json
        try: value = json.loads(response.content)
        except json.JSONDecodeError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid JSON output") from exc
        if not isinstance(value, dict): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output must be an object")
        return value
