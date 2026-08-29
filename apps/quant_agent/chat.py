"""In-terminal LLM chat: plain text typed at the ``bia>`` prompt talks to the model.

Reuses the platform's governed LLM stack (``OpenAICompatibleModel`` /
``AnthropicModel`` + ``GovernedLlmClient`` + ``ConversationService``) so chat
turns get retries, token budgeting and multi-turn context for free.
"""

from __future__ import annotations

from unicodedata import east_asian_width
from uuid import uuid4

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import (
    AnthropicModel,
    ChatModel,
    LlmError,
    LlmErrorCode,
    ModelResponse,
    OpenAICompatibleModel,
)
from active_agent_platform.llm_runtime import (
    ConversationService,
    GovernedLlmClient,
    LlmConfig,
)
from apps.quant_agent.model_picker import OLLAMA_PROVIDER

CHAT_TIMEOUT_SECONDS = 60.0
CHAT_CONVERSATION_ID = "bia-shell-chat"
DEFAULT_SYSTEM_PROMPT = "你是 BIA 类脑智能终端中的对话助手，回答使用中文，简洁、准确、直接。"


def build_chat_client(settings: Settings) -> GovernedLlmClient | None:
    """Assemble the governed chat client from settings; ``None`` when unconfigured."""
    key_ready = bool(settings.model_api_key) or settings.model_provider == OLLAMA_PROVIDER
    if not settings.model_url or not settings.model_name or not key_ready:
        return None
    api_key = settings.model_api_key or "ollama"
    model: ChatModel
    if settings.model_provider == "anthropic":
        model = AnthropicModel(
            base_url=settings.model_url, api_key=api_key,
            default_model=settings.model_name, provider=settings.model_provider,
        )
    else:
        model = OpenAICompatibleModel(
            base_url=settings.model_url, api_key=api_key,
            default_model=settings.model_name, provider=settings.model_provider,
        )
    config = LlmConfig(
        provider=settings.model_provider, model=settings.model_name,
        api_key_ref="bia-settings", timeout_seconds=CHAT_TIMEOUT_SECONDS,
        max_retries=3,
    )
    return GovernedLlmClient(model, config)


class ChatSession:
    """Multi-turn chat state bound to one configured model."""

    def __init__(
        self, client: GovernedLlmClient, *, label: str,
        system: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.client = client
        self.label = label
        self.system = system
        self.service = ConversationService(client)

    def clear(self) -> None:
        self.service = ConversationService(self.client)

    async def send(self, content: str) -> ModelResponse:
        return await self.service.send(
            CHAT_CONVERSATION_ID, content,
            correlation_id=uuid4().hex[:12], system=self.system,
        )


def describe_llm_error(error: LlmError) -> str:
    """Map governed LLM failures to actionable Chinese guidance, keeping detail."""
    guidance = {
        LlmErrorCode.AUTHENTICATION: "模型认证失败：API Key 无效或未授权，输入 /model 重新配置。",
        LlmErrorCode.RATE_LIMITED: "触发模型限流（或今日 token 预算用尽），请稍后再试。",
        LlmErrorCode.TIMEOUT: "模型响应超时，请稍后重试。",
        LlmErrorCode.UNAVAILABLE: "模型服务暂不可用：请检查 Endpoint 与网络，或稍后再试。",
        LlmErrorCode.INVALID_OUTPUT: "模型返回了无法解析的内容。",
    }
    base = guidance.get(error.code)
    if base is None:
        return f"对话失败：{error}"
    return f"{base}（服务端返回：{error}）"


def _display_width(text: str) -> int:
    """Visible terminal columns, counting CJK characters as two."""
    return sum(2 if east_asian_width(char) in {"F", "W"} else 1 for char in text)


def format_reply(
    response: ModelResponse, seconds: float, width: int = 0, *, color: bool = False,
) -> str:
    """Render an assistant reply plus its usage footer.

    With ``width`` set (TTY mode) the footer is right-aligned and dimmed so it
    reads as a quiet annotation instead of a line of conversation.
    """
    footer = (f"— {response.model} · 输入 {response.input_tokens}"
              f" / 输出 {response.output_tokens} tokens · {seconds:.1f}s")
    body = f"{response.content}\n"
    if width <= 0:
        return body + footer + "\n"
    pad = " " * max(1, width - _display_width(footer) - 1)
    if color:
        footer = f"\x1b[2m{footer}\x1b[0m"
    return f"{body}{pad}{footer}\n"
