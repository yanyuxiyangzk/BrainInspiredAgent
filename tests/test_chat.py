from __future__ import annotations

import io
from pathlib import Path
from urllib.error import HTTPError

import pytest

from active_agent_platform import llm as llm_module
from active_agent_platform.foundation import Settings
from active_agent_platform.llm import (
    AnthropicModel,
    ChatMessage,
    FakeChatModel,
    LlmError,
    LlmErrorCode,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleModel,
    StreamUsage,
)
from active_agent_platform.llm_runtime import GovernedLlmClient, LlmConfig
from apps.quant_agent.chat import (
    CHAT_CONVERSATION_ID,
    DEFAULT_SYSTEM_PROMPT,
    ChatInputError,
    ChatSession,
    WhitespaceNormalizer,
    blank_line_separator,
    build_chat_client,
    describe_llm_error,
    extract_images,
    format_footer,
    to_wsl_path,
    usage_note,
)


def _config() -> LlmConfig:
    return LlmConfig(
        provider="glm", model="glm-4-flash", api_key_ref="test",
        timeout_seconds=5, daily_token_budget=1000,
    )


def test_build_chat_client_requires_full_configuration() -> None:
    assert build_chat_client(Settings()) is None
    assert build_chat_client(Settings(model_url="https://x/v1", model_name="m")) is None
    ollama = build_chat_client(Settings(
        model_provider="ollama", model_url="http://localhost:11434/v1",
        model_name="llama3.2",
    ))
    assert ollama is not None and ollama.config.provider == "ollama"


def test_build_chat_client_selects_provider_adapter() -> None:
    glm = build_chat_client(Settings(
        model_provider="glm", model_url="https://open.bigmodel.cn/api/paas/v4",
        model_name="glm-4-flash", model_api_key="sk",
    ))
    anthropic = build_chat_client(Settings(
        model_provider="anthropic", model_url="https://api.anthropic.com",
        model_name="claude-3-5-sonnet-latest", model_api_key="sk",
    ))
    assert isinstance(glm.model, OpenAICompatibleModel)
    assert isinstance(anthropic.model, AnthropicModel)


@pytest.mark.asyncio
async def test_chat_session_keeps_multi_turn_context() -> None:
    model = FakeChatModel(["第一答", "第二答", "新话题答"])
    session = ChatSession(GovernedLlmClient(model, _config()), label="glm-4-flash · glm ✓")

    first = await session.send("一加一")
    second = await session.send("再加上二呢")
    assert (first.content, second.content) == ("第一答", "第二答")
    assert CHAT_CONVERSATION_ID == "bia-shell-chat"
    assert [message.role for message in model.requests[0].messages] == ["system", "user"]
    assert model.requests[0].messages[0].content == DEFAULT_SYSTEM_PROMPT
    assert [message.role for message in model.requests[1].messages] == [
        "system", "user", "assistant", "user",
    ]

    session.clear()
    third = await session.send("新话题")
    assert third.content == "新话题答"
    assert [message.role for message in model.requests[2].messages] == ["system", "user"]


def test_format_footer_omits_tokens_when_absent() -> None:
    with_tokens = format_footer(
        ModelResponse("内容", "glm-4-flash", "glm", "stop", 12, 34), 1.2,
    )
    without_tokens = format_footer(
        ModelResponse("内容", "glm-4-flash", "glm", "stop", 0, 0), 1.2,
    )
    assert with_tokens.startswith("— glm-4-flash · 输入 12 / 输出 34 tokens · 1.2s\n")
    assert without_tokens == "— glm-4-flash · 1.2s\n"
    assert "tokens" not in without_tokens


def test_usage_note_and_blank_separator_for_status_line() -> None:
    assert usage_note(ModelResponse("x", "m", "p", "stop", 37, 259), 11.04) == "↑37 ↓259 · 11.0s"
    assert usage_note(ModelResponse("x", "m", "p", "stop", 0, 0), 2.5) == "2.5s"
    assert blank_line_separator("", tty=False) == "\n"
    assert blank_line_separator("\n", tty=False) == ""
    assert blank_line_separator("", tty=True) == "\n\n"
    assert blank_line_separator("\n", tty=True) == "\n"
    assert blank_line_separator("\n\n", tty=True) == ""
    assert blank_line_separator("\n\n\n\n", tty=True) == "\x1b[1A\x1b[2K" * 2


def test_describe_llm_error_maps_guidance() -> None:
    assert "/model" in describe_llm_error(LlmError(LlmErrorCode.AUTHENTICATION, "x"))
    assert "限流" in describe_llm_error(LlmError(LlmErrorCode.RATE_LIMITED, "x"))
    unavailable = describe_llm_error(
        LlmError(LlmErrorCode.UNAVAILABLE, "model provider returned HTTP 400"),
    )
    assert unavailable.startswith("模型服务暂不可用") and "HTTP 400" in unavailable


class _FakeStreamResponse:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.closed = False

    def readline(self) -> bytes:
        return self._buffer.readline()

    def close(self) -> None:
        self.closed = True


def _stream_request() -> ModelRequest:
    return ModelRequest(
        messages=(ChatMessage("user", "hi"),),
        provider="glm", model="m", correlation_id="c",
    )


@pytest.mark.asyncio
async def test_chat_session_streams_through_on_delta() -> None:
    model = FakeChatModel(["这是一段足够长的回复"])
    deltas: list[str] = []
    session = ChatSession(GovernedLlmClient(model, _config()), label="x")
    response = await session.send("你好", on_delta=deltas.append)
    assert "".join(deltas) == "这是一段足够长的回复"
    assert len(deltas) > 1
    assert response.output_tokens == len("这是一段足够长的回复")


@pytest.mark.asyncio
async def test_iter_sse_data_parses_data_lines() -> None:
    stream = io.BytesIO(b'data: {"a":1}\n\n: keepalive\ndata: [DONE]\n')
    chunks = [data async for data in llm_module._iter_sse_data(stream)]
    assert chunks == ['{"a":1}', "[DONE]"]


@pytest.mark.asyncio
async def test_openai_stream_yields_deltas_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n'
        b"data: {\"choices\":[{\"delta\":{\"content\":\"\xe5\xa5\xbd\"}}],"
        b'"usage":{"prompt_tokens":3,"completion_tokens":5}}\n'
        b"data: [DONE]\n"
    )
    opened: list[object] = []
    holder: list[_FakeStreamResponse] = []

    def fake_urlopen(request: object, timeout: float = 60) -> _FakeStreamResponse:
        del timeout
        opened.append(request)
        response = _FakeStreamResponse(body)
        holder.append(response)
        return response

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
    chunks = [chunk async for chunk in model.stream_generate(_stream_request())]
    assert chunks == ["你", "好", StreamUsage(3, 5)]
    assert opened and "chat/completions" in str(getattr(opened[0], "full_url", ""))
    assert holder[0].closed


@pytest.mark.asyncio
async def test_openai_stream_maps_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float = 60) -> _FakeStreamResponse:
        del request, timeout
        raise HTTPError(
            "https://x/v1/chat/completions", 400, "Bad Request", None,
            io.BytesIO(b'{"error":{"message":"bad"}}'),
        )

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
    with pytest.raises(LlmError) as info:
        async for _ in model.stream_generate(_stream_request()):
            pass
    assert "HTTP 400" in str(info.value) and "bad" in str(info.value)


@pytest.mark.asyncio
async def test_anthropic_stream_parses_events(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n'
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n'
        b'data: {"type":"message_stop"}\n'
    )
    monkeypatch.setattr(
        llm_module, "urlopen",
        lambda request, timeout=60: _FakeStreamResponse(body),
    )
    model = AnthropicModel(base_url="https://x", api_key="k", default_model="m")
    chunks = [chunk async for chunk in model.stream_generate(_stream_request())]
    assert chunks == [StreamUsage(7, 0), "hi", StreamUsage(0, 2)]


@pytest.mark.asyncio
async def test_governed_stream_retries_before_first_delta() -> None:
    model = FakeChatModel([LlmError(LlmErrorCode.UNAVAILABLE, "flaky"), "恢复的回复"])
    client = GovernedLlmClient(model, _config())
    chunks = [chunk async for chunk in client.stream_generate(_stream_request())]
    text = "".join(chunk for chunk in chunks if isinstance(chunk, str))
    assert text == "恢复的回复" and len(model.requests) == 2


def test_to_wsl_path_maps_windows_drives() -> None:

    assert to_wsl_path("D:\\Program\\Weixin\\x.png") == "/mnt/d/Program/Weixin/x.png"
    assert to_wsl_path("D:/Program/Weixin/x.png") == "/mnt/d/Program/Weixin/x.png"
    assert to_wsl_path("/tmp/keep.png") == "/tmp/keep.png"


def test_extract_images_attaches_windows_drive_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "paste.png"
    image.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(
        "apps.quant_agent.chat.to_wsl_path",
        lambda p: str(image) if p.startswith("D:") else p,
    )
    text, images = extract_images("看 D:\\Weixin\\paste.png 好了")
    assert len(images) == 1 and images[0].startswith("data:image/png;base64,")
    assert text == "看 好了"


def test_extract_images_reads_existing_and_keeps_missing(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG-fake")
    text, images = extract_images(f"看看 [图片:{image}] 还有 missing.png 好了")
    assert len(images) == 1 and images[0].startswith("data:image/png;base64,")
    assert "shot" not in text and "missing.png" in text


def test_extract_images_rejects_oversized_file(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ChatInputError):
        extract_images(f"看 {big}")


def test_find_missing_images_reports_missing_paths(tmp_path: Path) -> None:
    from apps.quant_agent.chat import find_missing_images

    image = tmp_path / "ok.png"
    image.write_bytes(b"png")
    missing = find_missing_images(f"[图片:{image}] gone.png [图片:/tmp/nope.png]")
    assert missing == ["/tmp/nope.png", "gone.png"]


def test_whitespace_normalizer_unifies_blank_rhythm() -> None:

    normalizer = WhitespaceNormalizer()
    assert normalizer.feed("\n\n\n开头不留空行") == "开头不留空行"
    assert normalizer.feed("第一段\n\n\n\n") == "第一段"
    assert normalizer.feed("第二段") == "\n\n第二段"
    assert normalizer.feed("收尾\n\n") == "收尾"
    assert normalizer.flush() == "\n\n"

    run_away = WhitespaceNormalizer()
    assert run_away.feed("紧凑一次给足\n\n") == "紧凑一次给足"
    assert run_away.feed("\n\n\n第二段") == "\n\n第二段"


def test_markdown_formatter_renders_heading_bold_inline_and_list() -> None:
    from apps.quant_agent.chat import MarkdownStreamFormatter

    md = MarkdownStreamFormatter()
    out = md.feed("## 要点\n这是**重要**的 `pip install`\n- 第一条\n")
    assert "\x1b[1m\x1b[36m要点\x1b[0m" in out and "##" not in out
    assert "\x1b[1m重要\x1b[0m" in out and "**" not in out
    assert "\x1b[93mpip install\x1b[0m" in out and "`" not in out
    assert "• 第一条" in out and "- " not in out


def test_markdown_formatter_handles_code_fence_across_chunks() -> None:
    from apps.quant_agent.chat import MarkdownStreamFormatter

    md = MarkdownStreamFormatter()
    first = md.feed("```python\n")
    assert "── python ──" in first
    inside = md.feed("print(1)\n")
    assert "\x1b[2m  print(1)\x1b[0m" in inside
    last = md.feed("```\n之后\n")
    assert "```" not in last and "之后" in last


def test_markdown_formatter_holds_partial_line_until_newline() -> None:
    from apps.quant_agent.chat import MarkdownStreamFormatter

    md = MarkdownStreamFormatter()
    assert md.feed("这是**重") == ""
    out = md.feed("要**的\n")
    assert "\x1b[1m重要\x1b[0m" in out and "**" not in out
    assert md.flush() == ""


def test_markdown_formatter_renders_quote_hr_and_link() -> None:
    from apps.quant_agent.chat import MarkdownStreamFormatter

    md = MarkdownStreamFormatter()
    out = md.feed("> 引用内容\n---\n[文档](https://x)\n")
    assert "▏ 引用内容" in out
    assert "────────" in out and "---" not in out
    assert "文档 (https://x)" in out


@pytest.mark.asyncio
async def test_chat_session_sends_images() -> None:
    model = FakeChatModel(["好的"])
    session = ChatSession(GovernedLlmClient(model, _config()), label="x")
    await session.send("看图", images=("data:image/png;base64,aaa",))
    sent = model.requests[0].messages[-1]
    assert sent.images == ("data:image/png;base64,aaa",)


@pytest.mark.asyncio
async def test_openai_payload_includes_image_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float = 60) -> _FakeStreamResponse:
        del timeout
        captured["body"] = llm_module.json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return _FakeStreamResponse(b"data: [DONE]\n")

    monkeypatch.setattr(llm_module, "urlopen", fake_urlopen)
    model = OpenAICompatibleModel(base_url="https://x/v1", api_key="k", default_model="m")
    request = ModelRequest(
        messages=(ChatMessage("user", "看图", images=("data:image/png;base64,aaa",)),),
        provider="glm", model="m", correlation_id="c",
    )
    [chunk async for chunk in model.stream_generate(request)]
    content = captured["body"]["messages"][0]["content"]  # type: ignore[index]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"
