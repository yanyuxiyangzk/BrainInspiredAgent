"""Recovery-path tests for the governed LLM runtime (R-series contract)."""
from __future__ import annotations

import pytest

from active_agent_platform.llm import (
    ChatMessage,
    FakeChatModel,
    LlmError,
    LlmErrorCode,
    ModelRequest,
)
from active_agent_platform.llm_runtime import (
    Conversation,
    ConversationService,
    GovernedLlmClient,
    LlmBudget,
    LlmConfig,
)


def make_client(responses: list[object], **config_overrides: object) -> GovernedLlmClient:
    config_values: dict[str, object] = {
        "provider": "fake",
        "model": "fake-1",
        "api_key_ref": "env:KEY",
        "max_retries": 2,
        "timeout_seconds": 5.0,
    }
    config_values.update(config_overrides)
    return GovernedLlmClient(FakeChatModel(responses), LlmConfig(**config_values))  # type: ignore[arg-type]


def make_request() -> ModelRequest:
    return ModelRequest((ChatMessage("user", "你好"),), "fake", "fake-1", "corr-1")


def test_llm_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="invalid LLM configuration"):
        LlmConfig(provider="", model="m", api_key_ref="k")
    with pytest.raises(ValueError, match="invalid LLM configuration"):
        LlmConfig(provider="p", model="m", api_key_ref="k", max_retries=-1)
    with pytest.raises(ValueError, match="invalid LLM configuration"):
        LlmConfig(provider="p", model="m", api_key_ref="k", timeout_seconds=0)
    with pytest.raises(ValueError, match="invalid LLM configuration"):
        LlmConfig(provider="p", model="m", api_key_ref="k", daily_token_budget=0)


def test_llm_budget_rejects_invalid_limit_and_overrun() -> None:
    with pytest.raises(ValueError, match="token limit"):
        LlmBudget(0)
    budget = LlmBudget(10)
    budget.reserve(4)
    with pytest.raises(LlmError) as overrun:
        budget.reserve(7)
    assert overrun.value.code is LlmErrorCode.RATE_LIMITED
    with pytest.raises(LlmError) as negative:
        budget.reserve(-1)
    assert negative.value.code is LlmErrorCode.RATE_LIMITED


@pytest.mark.asyncio
async def test_generate_records_usage_and_returns_response() -> None:
    client = make_client(["第一次回复"])
    response = await client.generate(make_request())
    assert response.content == "第一次回复"
    assert client.usage.requests == 1  # Fake 非流式响应不携带 token 统计


@pytest.mark.asyncio
async def test_generate_retries_transient_failures_then_succeeds() -> None:
    failure = LlmError(LlmErrorCode.UNAVAILABLE, "provider down")
    client = make_client([failure, failure, "恢复后的回复"], max_retries=2)
    response = await client.generate(make_request())
    assert response.content == "恢复后的回复"
    assert len(client.model.requests) == 3  # 前两次失败，第三次成功


@pytest.mark.asyncio
async def test_generate_raises_after_exhausting_retries() -> None:
    failure = LlmError(LlmErrorCode.UNAVAILABLE, "provider down")
    client = make_client([failure, failure, failure], max_retries=2)
    with pytest.raises(LlmError) as exhausted:
        await client.generate(make_request())
    assert exhausted.value.code is LlmErrorCode.UNAVAILABLE
    assert len(client.model.requests) == 3


@pytest.mark.asyncio
async def test_generate_does_not_retry_non_retryable_errors() -> None:
    invalid = LlmError(LlmErrorCode.INVALID_OUTPUT, "bad output")
    client = make_client([invalid, "不应到达"], max_retries=2)
    with pytest.raises(LlmError) as raised:
        await client.generate(make_request())
    assert raised.value.code is LlmErrorCode.INVALID_OUTPUT
    assert len(client.model.requests) == 1


def test_conversation_append_is_persistent_context() -> None:
    conversation = Conversation("c-1")
    grown = conversation.append(ChatMessage("user", "问题"), ChatMessage("assistant", "回答"))
    assert len(grown.messages) == 2
    assert conversation.messages == ()
    assert grown.conversation_id == "c-1"


@pytest.mark.asyncio
async def test_conversation_service_send_keeps_context_and_system() -> None:
    service = ConversationService(make_client(["回复一", "回复二"]))
    first = await service.send("c-1", "第一问", correlation_id="corr-1", system="你是量化助手")
    assert first.content == "回复一"
    second = await service.send("c-1", "第二问", correlation_id="corr-2")
    assert second.content == "回复二"

    session = service.get("c-1")
    roles = [message.role for message in session.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert session.messages[0].content == "你是量化助手"

    fresh = service.get("c-2")
    assert fresh.messages == ()  # 新会话空启动
