"""Domain-neutral contracts for governed language-model calls."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
import asyncio
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


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


class OpenAICompatibleModel:
    """OpenAI-compatible ``/chat/completions`` adapter using stdlib HTTP."""
    capabilities = ModelCapabilities(structured_output=True, tool_calling=True)

    def __init__(self, *, base_url: str, api_key: str, default_model: str | None = None,
                 provider: str = "openai-compatible") -> None:
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key, self.default_model, self.provider = api_key, default_model, provider

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request.model or self.default_model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.seed is not None: payload["seed"] = request.seed
        if request.response_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "response", "schema": request.response_schema}}
        body = json.dumps(payload).encode()
        http_request = Request(self.base_url + "/chat/completions", data=body, method="POST",
                               headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(self._read, http_request), request.timeout_seconds)
            document = json.loads(raw)
            choice = document["choices"][0]
            message = choice["message"]["content"]
            usage = document.get("usage", {})
            return ModelResponse(str(message), str(document.get("model", request.model)), self.provider,
                                 str(choice.get("finish_reason", "stop")), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        except asyncio.TimeoutError as exc: raise LlmError(LlmErrorCode.TIMEOUT, "model request timed out") from exc
        except HTTPError as exc:
            code = LlmErrorCode.AUTHENTICATION if exc.code in (401, 403) else LlmErrorCode.RATE_LIMITED if exc.code == 429 else LlmErrorCode.UNAVAILABLE
            raise LlmError(code, f"model provider returned HTTP {exc.code}") from exc
        except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model provider unavailable") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid model response") from exc

    @staticmethod
    def _read(request: Request) -> str:
        with urlopen(request, timeout=60) as response:  # noqa: S310 - configured endpoint
            return response.read().decode("utf-8")

    async def generate_structured(self, request: ModelRequest) -> Mapping[str, object]:
        response = await self.generate(request)
        try: value = json.loads(response.content)
        except json.JSONDecodeError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid JSON output") from exc
        if not isinstance(value, dict): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output must be an object")
        return value
