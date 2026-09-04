"""HTTP adapter tests: OpenAI-compatible and Anthropic models over a local server."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from active_agent_platform.llm import (
    AnthropicModel,
    ChatMessage,
    LlmError,
    LlmErrorCode,
    ModelRequest,
    OpenAICompatibleModel,
)

REQUEST = ModelRequest(
    messages=(ChatMessage("system", "be brief"), ChatMessage("user", "hello")),
    provider="test", model="test-model", correlation_id="corr-1", timeout_seconds=5,
)


class _FakeEndpoint(BaseHTTPRequestHandler):
    """Programmable fake model endpoint: set ``respond`` per test."""

    respond: Callable[[BaseHTTPRequestHandler, bytes], None] = lambda h, b: h.send_response(500)

    def log_message(self, *args: object) -> None:
        del args

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).respond(self, body)


def _server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _FakeEndpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _json(handler: BaseHTTPRequestHandler, payload: dict[str, object]) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_openai_model_sends_payload_and_parses_response() -> None:
    def respond(handler: BaseHTTPRequestHandler, body: bytes) -> None:
        captured_store.append(json.loads(body))
        _json(handler, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                        "model": "served-model", "usage": {"prompt_tokens": 3,
                                                           "completion_tokens": 5}})

    captured_store: list[dict[str, object]] = []
    server, url = _server()
    _FakeEndpoint.respond = respond
    try:
        model = OpenAICompatibleModel(base_url=url, api_key="k", default_model="fallback")
        response = asyncio.run(model.generate(REQUEST))
        assert response.content == "hi" and response.model == "served-model"
        assert response.input_tokens == 3 and response.output_tokens == 5
        sent = captured_store[0]
        assert sent["model"] == "test-model"
        assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    finally:
        server.shutdown()


def test_openai_model_maps_http_errors() -> None:
    server, url = _server()
    _FakeEndpoint.respond = lambda h, b: h.send_error(429, "rate limited")
    try:
        model = OpenAICompatibleModel(base_url=url, api_key="k")
        with pytest.raises(LlmError) as error:
            asyncio.run(model.generate(REQUEST))
        assert error.value.code is LlmErrorCode.RATE_LIMITED
    finally:
        server.shutdown()


def test_openai_model_rejects_invalid_endpoint_url() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleModel(base_url="", api_key="k")


def test_openai_model_maps_invalid_response_shape() -> None:
    server, url = _server()
    _FakeEndpoint.respond = lambda h, b: _json(h, {"unexpected": True})
    try:
        model = OpenAICompatibleModel(base_url=url, api_key="k")
        with pytest.raises(LlmError) as error:
            asyncio.run(model.generate(REQUEST))
        assert error.value.code is LlmErrorCode.INVALID_OUTPUT
    finally:
        server.shutdown()


def test_openai_model_structured_output_and_vision_messages() -> None:
    captured_store: list[dict[str, object]] = []

    def respond(handler: BaseHTTPRequestHandler, body: bytes) -> None:
        captured_store.append(json.loads(body))
        _json(handler, {"choices": [{"message": {"content": "{\"ok\": true}"}}]})

    server, url = _server()
    _FakeEndpoint.respond = respond
    try:
        model = OpenAICompatibleModel(base_url=url, api_key="k")
        request = ModelRequest(
            messages=(ChatMessage("user", "see", images=("data:image/png;base64,AA",)),),
            provider="test", model="m", correlation_id="c",
            response_schema={"type": "object"},
        )
        value = asyncio.run(model.generate_structured(request))
        assert value == {"ok": True}
        message_payload = captured_store[0]["messages"][0]["content"]
        assert isinstance(message_payload, list)  # vision content array
        assert captured_store[0]["response_format"]["type"] == "json_schema"
    finally:
        server.shutdown()


def test_anthropic_model_generates_and_maps_errors() -> None:
    server, url = _server()

    def respond(handler: BaseHTTPRequestHandler, body: bytes) -> None:
        payload = json.loads(body)
        assert payload["max_tokens"] == 4096 and payload["system"] == "be brief"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        _json(handler, {"content": [{"text": "bonjour"}], "model": "served",
                        "stop_reason": "end_turn", "usage": {"input_tokens": 2,
                                                             "output_tokens": 4}})

    _FakeEndpoint.respond = respond
    try:
        model = AnthropicModel(base_url=url, api_key="k", provider="anthropic")
        response = asyncio.run(model.generate(REQUEST))
        assert response.content == "bonjour" and response.output_tokens == 4
    finally:
        server.shutdown()

    auth_server, auth_url = _server()
    _FakeEndpoint.respond = lambda h, b: h.send_error(401, "unauthorized")
    try:
        model = AnthropicModel(base_url=auth_url, api_key="k", provider="anthropic")
        with pytest.raises(LlmError) as error:
            asyncio.run(model.generate(REQUEST))
        assert error.value.code is LlmErrorCode.AUTHENTICATION
    finally:
        auth_server.shutdown()


def test_anthropic_model_maps_invalid_response() -> None:
    server, url = _server()
    _FakeEndpoint.respond = lambda h, b: _json(h, {"nope": []})
    try:
        model = AnthropicModel(base_url=url, api_key="k", provider="anthropic")
        with pytest.raises(LlmError) as error:
            asyncio.run(model.generate(REQUEST))
        assert error.value.code is LlmErrorCode.INVALID_OUTPUT
    finally:
        server.shutdown()
