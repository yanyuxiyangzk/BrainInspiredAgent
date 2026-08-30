"""In-terminal LLM chat: plain text typed at the ``bia>`` prompt talks to the model.

Reuses the platform's governed LLM stack (``OpenAICompatibleModel`` /
``AnthropicModel`` + ``GovernedLlmClient`` + ``ConversationService``) so chat
turns get retries, token budgeting and multi-turn context for free.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable
from pathlib import Path
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
IMAGE_PLACEHOLDER = re.compile(r"\[图片:([^\]\s]+)\]")
IMAGE_PATTERN = re.compile(r"[^\s\[\]]+?\.(?:png|jpe?g|webp|gif)", re.IGNORECASE)
IMAGE_MIME = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ChatInputError(RuntimeError):
    """Invalid chat input, such as an over-sized image attachment."""


def to_wsl_path(path: str) -> str:
    """Map windows-style paths (``D:\\foo\\bar.png``) onto ``/mnt/d/...``.

    Also strips the ``图片:`` placeholder prefix so bare-pattern matches
    normalize to the same real path.
    """
    posix = re.sub(r"^图片[:：]", "", path.replace("\\", "/").strip())
    if len(posix) > 2 and posix[1] == ":":
        return f"/mnt/{posix[0].lower()}{posix[2:]}"
    return posix


def image_data_url(path: str) -> str:
    """Encode one image file as a base64 data URL, enforcing the size cap."""
    path = to_wsl_path(path)
    if not os.path.isfile(path):
        raise ChatInputError(f"图片不存在：{path}")
    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise ChatInputError(f"图片 {path} 超过 5MB 上限，请压缩后再试")
    suffix = os.path.splitext(path)[1].lower().lstrip(".")
    mime = IMAGE_MIME.get(suffix, "png")
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{data}"


def extract_images(text: str) -> tuple[str, tuple[str, ...]]:
    """Pull existing image file paths out of ``text`` as base64 data URLs.

    ``[图片:path]`` placeholders from the clipboard handler are checked first;
    bare paths that do not exist on disk stay untouched so half-typed words
    survive.
    """
    images: list[str] = []

    def _collect(path: str) -> str:
        normalized = to_wsl_path(path)
        if not os.path.isfile(normalized):
            return path
        images.append(image_data_url(normalized))
        return ""

    text = IMAGE_PLACEHOLDER.sub(lambda match: _collect(match.group(1)), text)
    cleaned = IMAGE_PATTERN.sub(lambda match: _collect(match.group(0)), text)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    return cleaned, tuple(images)


def find_missing_images(text: str) -> list[str]:
    """Image-looking paths from ``text`` that do not exist on disk."""
    missing: list[str] = []
    for match in IMAGE_PLACEHOLDER.finditer(text):
        path = match.group(1)
        if not os.path.isfile(to_wsl_path(path)) and path not in missing:
            missing.append(path)
    for match in IMAGE_PATTERN.finditer(text):
        normalized = to_wsl_path(match.group(0))
        if not os.path.isfile(normalized) and normalized not in missing:
            missing.append(normalized)
    return missing


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

    async def send(
        self, content: str, on_delta: Callable[[str], None] | None = None,
        images: tuple[str, ...] = (),
    ) -> ModelResponse:
        """Send one turn; when ``on_delta`` is given the reply streams through it."""
        return await self.service.send_streaming(
            CHAT_CONVERSATION_ID, content,
            correlation_id=uuid4().hex[:12], system=self.system, on_delta=on_delta,
            images=images,
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


def format_footer(
    response: ModelResponse, seconds: float, width: int = 0, *, color: bool = False,
) -> str:
    """Render the usage footer; right-aligned and dimmed in TTY mode."""
    stats = (f"输入 {response.input_tokens} / 输出 {response.output_tokens} tokens · "
             if response.input_tokens or response.output_tokens else "")
    footer = f"— {response.model} · {stats}{seconds:.1f}s"
    if width <= 0:
        return footer + "\n"
    pad = " " * max(1, width - _display_width(footer) - 1)
    if color:
        footer = f"\x1b[2m{footer}\x1b[0m"
    return f"{pad}{footer}\n"


def format_reply(
    response: ModelResponse, seconds: float, width: int = 0, *, color: bool = False,
) -> str:
    """Render a whole reply (content plus footer) for non-streaming callers."""
    return f"{response.content}\n" + format_footer(response, seconds, width, color=color)
