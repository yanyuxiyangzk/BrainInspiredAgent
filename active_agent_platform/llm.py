"""Domain-neutral contracts for governed language-model calls."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPResponse
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
    images: tuple[str, ...] = ()

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


@dataclass(frozen=True, slots=True)
class StreamUsage:
    """Token accounting reported inside a streamed response."""

    input_tokens: int = 0
    output_tokens: int = 0


class ChatModel(Protocol):
    capabilities: ModelCapabilities
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    def stream_generate(self, request: ModelRequest) -> AsyncIterator[StreamUsage | str]: ...


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

    async def stream_generate(self, request: ModelRequest) -> AsyncIterator[StreamUsage | str]:
        self.requests.append(request)
        try: value = next(self._responses)
        except StopIteration as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "no fake response") from exc
        if isinstance(value, Exception): raise value
        text = value if isinstance(value, str) else str(value)
        for index in range(0, len(text), 4):
            yield text[index:index + 4]
        yield StreamUsage(len(request.messages), max(1, len(text)))

    async def generate_structured(self, request: ModelRequest) -> Mapping[str, object]:
        response = await self.generate(request)
        if not isinstance(response.content, str): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output is not text")
        import json
        try: value = json.loads(response.content)
        except json.JSONDecodeError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid JSON output") from exc
        if not isinstance(value, dict): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output must be an object")
        return value


def _http_error_to_llm(exc: HTTPError) -> LlmError:
    """Map an HTTP error to the governed error codes, keeping the server body."""
    code = (LlmErrorCode.AUTHENTICATION if exc.code in (401, 403)
            else LlmErrorCode.RATE_LIMITED if exc.code == 429 else LlmErrorCode.UNAVAILABLE)
    detail = f"model provider returned HTTP {exc.code}"
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - the body is best-effort context
        body = ""
    if body:
        detail = f"{detail}: {body[:200]}"
    return LlmError(code, detail)


async def _iter_sse_data(response: HTTPResponse) -> AsyncIterator[str]:
    """Yield the payload of SSE ``data:`` lines, skipping blanks and comments."""
    while True:
        line = await asyncio.to_thread(response.readline)
        if not line:
            return
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("data:"):
            yield text[5:].strip()


async def _open_stream(request: Request, timeout: float) -> HTTPResponse:
    """Open a blocking SSE connection off the event loop, mapping errors."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(urlopen, request, timeout=timeout), timeout)
    except TimeoutError as exc: raise LlmError(LlmErrorCode.TIMEOUT, "model request timed out") from exc
    except HTTPError as exc: raise _http_error_to_llm(exc) from exc
    except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model provider unavailable") from exc


class OpenAICompatibleModel:
    """OpenAI-compatible ``/chat/completions`` adapter using stdlib HTTP."""
    capabilities = ModelCapabilities(structured_output=True, tool_calling=True)

    def __init__(self, *, base_url: str, api_key: str, default_model: str | None = None,
                 provider: str = "openai-compatible") -> None:
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key, self.default_model, self.provider = api_key, default_model, provider

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, object]:
        """Plain text when possible, OpenAI vision content array with images."""
        if not message.images:
            return {"role": message.role, "content": message.content}
        content: list[dict[str, object]] = [{"type": "text", "text": message.content}]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in message.images)
        return {"role": message.role, "content": content}

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request.model or self.default_model,
            "messages": [self._message_payload(m) for m in request.messages],
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
        except TimeoutError as exc: raise LlmError(LlmErrorCode.TIMEOUT, "model request timed out") from exc
        except HTTPError as exc: raise _http_error_to_llm(exc) from exc
        except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model provider unavailable") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid model response") from exc

    @staticmethod
    def _read(request: Request) -> str:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")

    async def generate_structured(self, request: ModelRequest) -> Mapping[str, object]:
        response = await self.generate(request)
        try: value = json.loads(response.content)
        except json.JSONDecodeError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid JSON output") from exc
        if not isinstance(value, dict): raise LlmError(LlmErrorCode.INVALID_OUTPUT, "structured output must be an object")
        return value

    async def stream_generate(self, request: ModelRequest) -> AsyncIterator[StreamUsage | str]:
        """Stream ``/chat/completions`` deltas plus a trailing usage report."""
        payload: dict[str, object] = {
            "model": request.model or self.default_model,
            "messages": [self._message_payload(m) for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.seed is not None: payload["seed"] = request.seed
        body = json.dumps(payload).encode()
        http_request = Request(self.base_url + "/chat/completions", data=body, method="POST",
                               headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        response = await _open_stream(http_request, request.timeout_seconds)
        try:
            async for data in _iter_sse_data(response):
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except ValueError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid stream chunk") from exc
                if not isinstance(chunk, dict):
                    raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid stream chunk")
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    yield str(delta)
                usage = chunk.get("usage")
                if isinstance(usage, dict) and usage:
                    yield StreamUsage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model stream interrupted") from exc
        finally:
            await asyncio.to_thread(response.close)


class AnthropicModel(OpenAICompatibleModel):
    """Anthropic Messages API adapter with the provider-neutral contract."""
    capabilities = ModelCapabilities(tool_calling=True)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        system = "\n".join(m.content for m in request.messages if m.role == "system")
        messages = [{"role": "user" if m.role == "tool" else m.role, "content": m.content}
                    for m in request.messages if m.role != "system"]
        payload: dict[str, object] = {"model": request.model, "max_tokens": 4096, "messages": messages}
        if system: payload["system"] = system
        body = json.dumps(payload).encode()
        http_request = Request(self.base_url + "/messages", data=body, method="POST", headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(self._read, http_request), request.timeout_seconds)
        except TimeoutError as exc: raise LlmError(LlmErrorCode.TIMEOUT, "model request timed out") from exc
        except HTTPError as exc: raise _http_error_to_llm(exc) from exc
        except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model provider unavailable") from exc
        try:
            document = json.loads(raw); content = document["content"][0]["text"]; usage = document.get("usage", {})
            return ModelResponse(str(content), str(document.get("model", request.model)), self.provider,
                                 str(document.get("stop_reason", "end_turn")), int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
        except (KeyError, IndexError, TypeError, ValueError) as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid model response") from exc

    async def stream_generate(self, request: ModelRequest) -> AsyncIterator[StreamUsage | str]:
        """Stream Anthropic Messages deltas plus token accounting events."""
        system = "\n".join(m.content for m in request.messages if m.role == "system")
        messages = [{"role": "user" if m.role == "tool" else m.role, "content": m.content}
                    for m in request.messages if m.role != "system"]
        payload: dict[str, object] = {"model": request.model, "max_tokens": 4096,
                                      "messages": messages, "stream": True}
        if system: payload["system"] = system
        body = json.dumps(payload).encode()
        http_request = Request(self.base_url + "/messages", data=body, method="POST", headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        response = await _open_stream(http_request, request.timeout_seconds)
        try:
            async for data in _iter_sse_data(response):
                if not data:
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError as exc: raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid stream chunk") from exc
                if not isinstance(chunk, dict):
                    raise LlmError(LlmErrorCode.INVALID_OUTPUT, "invalid stream chunk")
                kind = chunk.get("type")
                if kind == "content_block_delta":
                    delta = (chunk.get("delta") or {}).get("text")
                    if delta:
                        yield str(delta)
                elif kind == "message_start":
                    usage = (chunk.get("message") or {}).get("usage") or {}
                    if usage.get("input_tokens"):
                        yield StreamUsage(int(usage["input_tokens"]), 0)
                elif kind == "message_delta":
                    usage = chunk.get("usage") or {}
                    if usage.get("output_tokens"):
                        yield StreamUsage(0, int(usage["output_tokens"]))
                elif kind == "message_stop":
                    return
        except (URLError, OSError) as exc: raise LlmError(LlmErrorCode.UNAVAILABLE, "model stream interrupted") from exc
        finally:
            await asyncio.to_thread(response.close)
