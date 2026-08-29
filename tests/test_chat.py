from __future__ import annotations

import pytest

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import (
    AnthropicModel,
    FakeChatModel,
    LlmError,
    LlmErrorCode,
    ModelResponse,
    OpenAICompatibleModel,
)
from active_agent_platform.llm_runtime import GovernedLlmClient, LlmConfig
from apps.quant_agent.chat import (
    CHAT_CONVERSATION_ID,
    DEFAULT_SYSTEM_PROMPT,
    ChatSession,
    build_chat_client,
    describe_llm_error,
    format_reply,
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


def test_format_reply_includes_usage_footer() -> None:
    text = format_reply(
        ModelResponse("内容", "glm-4-flash", "glm", "stop", 12, 34), 1.234,
    )
    assert text.startswith("内容\n")
    assert "输入 12 / 输出 34 tokens" in text and "1.2s" in text


def test_format_reply_right_aligns_footer_in_tty() -> None:
    from apps.quant_agent.chat import _display_width

    text = format_reply(
        ModelResponse("内容", "glm-4-flash", "glm", "stop", 12, 34), 1.2, 60, color=True,
    )
    first, footer = text.splitlines()
    assert first == "内容"
    assert footer.startswith(" ") and "\x1b[2m" in footer and footer.endswith("\x1b[0m")
    plain = footer.replace("\x1b[2m", "").replace("\x1b[0m", "")
    assert 40 <= _display_width(plain) <= 59


def test_describe_llm_error_maps_guidance() -> None:
    assert "/model" in describe_llm_error(LlmError(LlmErrorCode.AUTHENTICATION, "x"))
    assert "限流" in describe_llm_error(LlmError(LlmErrorCode.RATE_LIMITED, "x"))
    unavailable = describe_llm_error(
        LlmError(LlmErrorCode.UNAVAILABLE, "model provider returned HTTP 400"),
    )
    assert unavailable.startswith("模型服务暂不可用") and "HTTP 400" in unavailable
