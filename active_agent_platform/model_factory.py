"""Build a real or deterministic model from environment-backed settings."""
from .foundation import Settings
from .llm import AnthropicModel, ChatModel, FakeChatModel, OpenAICompatibleModel

def build_model(settings: Settings) -> ChatModel:
    provider = settings.model_provider.lower().replace("_", "-")
    if not settings.model_url or not settings.model_name or not settings.model_api_key:
        return FakeChatModel(["{}"], provider="fake", model="fake-1")
    if provider in {"anthropic", "claude"}:
        base = settings.model_url[:-3] if settings.model_url.endswith("/v1") else settings.model_url
        return AnthropicModel(base_url=base, api_key=settings.model_api_key, default_model=settings.model_name, provider="anthropic")
    if provider in {"openai", "openai-compatible", "glm", "zhipu", "deepseek", "qwen", "ollama", "vllm"}:
        return OpenAICompatibleModel(base_url=settings.model_url, api_key=settings.model_api_key, default_model=settings.model_name, provider=provider)
    raise ValueError(f"unsupported model provider: {settings.model_provider}")
