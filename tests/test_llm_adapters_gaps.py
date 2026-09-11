"""Branch-completion tests for the provider-neutral LLM adapters (R-series)."""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Self

import pytest

from active_agent_platform import llm as llm_module
from active_agent_platform.llm import (
    AnthropicModel,
    ChatMessage,
    FakeChatModel,
    LlmError,
    LlmErrorCode,
    ModelRequest,
    OpenAICompatibleModel,
    _http_error_to_llm,
)


class _FakeStreamResponse:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.closed = False

    def readline(self) -> bytes:
        return self._buffer.readline()

    def close(self) -> None:
        self.closed = True


def _request(**overrides: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "messages": (ChatMessage("user", "hi"),),
        "provider": "glm",
        "model": "m",
        "correlation_id": "c",
    }
    values.update(overrides)
    return ModelRequest(**values)


def test_chat_message_rejects_unknown_role_and_empty_content() -> None:
    with pytest.raises(ValueError, match="invalid chat message"):
        ChatMessage("narrator", "hello")
    with pytest.raises(ValueError, match="invalid chat message"):
        ChatMessage("user", "")


def test_model_request_rejects_missing_identity_and_bad_bounds() -> None:
    with pytest.raises(ValueError, match="identity and messages"):
        _request(messages=())
    with pytest.raises(ValueError, match="identity and messages"):
        _request(correlation_id="")
    with pytest.raises(ValueError, match="bounds"):
        _request(temperature=3.0)
    with pytest.raises(ValueError, match="bounds"):
        _request(timeout_seconds=0)


def test_http_error_mapping_covers_status_families_and_bodies() -> None:
    def http_error(code: int, body: bytes | None) -> LlmError:
        payload = None if body is None else io.BytesIO(body)
        return _http_error_to_llm(
            urllib.error.HTTPError("https://x", code, "oops", None, payload)
        )

    assert http_error(401, b'{"message":"denied"}').code is LlmErrorCode.AUTHENTICATION
    assert "denied" in str(http_error(401, b'{"message":"denied"}'))
    assert http_error(429, b"slow down").code is LlmErrorCode.RATE_LIMITED
    assert "HTTP 500" in str(http_error(500, b"boom"))
    assert http_error(500, None).code is LlmErrorCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_fake_structured_output_rejects_non_json_and_non_object() -> None:
    model = FakeChatModel(["not-json", "[1,2]"])
    request = _request()
    with pytest.raises(LlmError, match="invalid JSON output"):
        await model.generate_structured(request)
    with pytest.raises(LlmError, match="must be an object"):
        await model.generate_structured(request)


@pytest.mark.asyncio
async def test_open_stream_maps_timeout_and_unreachable_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout_urlopen(request: object, timeout: float = 60) -> _FakeStreamResponse:
        raise TimeoutError()

    def unreachable_urlopen(request: object, timeout: float = 60) -> _FakeStreamResponse:
        raise urllib.error.URLError("no route")

    model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
    monkeypatch.setattr(llm_module, "urlopen", timeout_urlopen)
    with pytest.raises(LlmError, match="timed out"):
        async for _ in model.stream_generate(_request()):
            pass
    monkeypatch.setattr(llm_module, "urlopen", unreachable_urlopen)
    with pytest.raises(LlmError, match="unavailable"):
        async for _ in model.stream_generate(_request()):
            pass


@pytest.mark.asyncio
async def test_openai_structured_output_rejects_non_object() -> None:
    class FixedModel(OpenAICompatibleModel):
        async def generate(self, request: ModelRequest):  # type: ignore[override]
            return llm_module.ModelResponse("123", "m", "glm", "stop")

    with pytest.raises(LlmError, match="must be an object"):
        await FixedModel(base_url="https://x/v1", api_key="k").generate_structured(_request())


@pytest.mark.asyncio
async def test_openai_stream_ignores_empty_choices_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bytes] = []

    def fake_urlopen(request: Any, timeout: float = 60) -> _FakeStreamResponse:
        captured.append(request.data)
        return _FakeStreamResponse(
            b'data: {"choices":[]}\n'
            b'data: {"choices":[{"delta":{}}],"usage":{}}\n'
            b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n'
            b"data: [DONE]\n"
            b'data: {"after-done":true}\n'
        )

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
    chunks = [chunk async for chunk in model.stream_generate(_request(seed=7))]
    assert chunks == ["好"]  # [DONE] 终止流，其后的数据被忽略
    assert json.loads(captured[0])["seed"] == 7


@pytest.mark.asyncio
async def test_openai_stream_rejects_malformed_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    for body in (b"data: not-json\n", b'data: ["array"]\n'):
        monkeypatch.setattr(
            llm_module, "urlopen", lambda request, timeout=60, body=body: _FakeStreamResponse(body)
        )
        model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
        with pytest.raises(LlmError, match="invalid stream chunk"):
            async for _ in model.stream_generate(_request()):
                pass


@pytest.mark.asyncio
async def test_anthropic_generate_sends_system_and_maps_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bytes] = []

    def fake_urlopen(request: Any, timeout: float = 60) -> Any:
        captured.append(request.data)
        raise urllib.error.HTTPError("https://x", 401, "no key", None, io.BytesIO(b"bad key"))

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = AnthropicModel(base_url="https://x", api_key="k", default_model="m")
    request = _request(
        messages=(ChatMessage("system", "你是量化助手"), ChatMessage("user", "hi"))
    )
    with pytest.raises(LlmError) as auth:
        await model.generate(request)
    assert auth.value.code is LlmErrorCode.AUTHENTICATION
    body = json.loads(captured[0])
    assert body["system"] == "你是量化助手"
    assert all(message["role"] != "system" for message in body["messages"])


@pytest.mark.asyncio
async def test_anthropic_generate_maps_timeout_and_invalid_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout_urlopen(request: Any, timeout: float = 60) -> Any:
        raise TimeoutError()

    def broken_urlopen(request: Any, timeout: float = 60) -> Any:
        class _Response:
            def read(self) -> bytes:
                return b'{"unexpected": true}'

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return _Response()

    model = AnthropicModel(base_url="https://x", api_key="k", default_model="m")
    monkeypatch.setattr(llm_module, "urlopen", timeout_urlopen)
    with pytest.raises(LlmError, match="timed out"):
        await model.generate(_request())

    monkeypatch.setattr(llm_module, "urlopen", broken_urlopen)
    with pytest.raises(LlmError, match="invalid model response"):
        await model.generate(_request())


@pytest.mark.asyncio
async def test_anthropic_stream_ignores_silent_events_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        b"data: \n"
        b'data: {"type":"content_block_delta","delta":{}}\n'
        b'data: {"type":"message_start","message":{"usage":{}}}\n'
        b'data: {"type":"message_delta","usage":{}}\n'
        b'data: {"type":"content_block_delta","delta":{"text":"\xe5\xa5\xbd"}}\n'
        b'data: {"type":"message_stop"}\n'
        b'data: {"type":"after-stop"}\n'
    )
    holder: list[_FakeStreamResponse] = []

    def fake_urlopen(request: Any, timeout: float = 60) -> _FakeStreamResponse:
        response = _FakeStreamResponse(body)
        holder.append(response)
        return response

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = AnthropicModel(base_url="https://x", api_key="k", default_model="m")
    chunks = [chunk async for chunk in model.stream_generate(_request())]
    assert chunks == ["好"]  # 空 usage/空 delta 不产出，message_stop 终止
    assert holder[0].closed


@pytest.mark.asyncio
async def test_anthropic_stream_rejects_malformed_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for body in (b"data: broken\n", b'data: ["array"]\n'):
        monkeypatch.setattr(
            llm_module, "urlopen", lambda request, timeout=60, body=body: _FakeStreamResponse(body)
        )
        model = AnthropicModel(base_url="https://x", api_key="k", default_model="m")
        with pytest.raises(LlmError, match="invalid stream chunk"):
            async for _ in model.stream_generate(_request()):
                pass
