"""Tests for the environment-backed model factory (R01 provider selection)."""
from __future__ import annotations

import pytest

from active_agent_platform.foundation import Settings
from active_agent_platform.llm import FakeChatModel, OpenAICompatibleModel
from active_agent_platform.model_factory import build_model


def test_build_model_falls_back_to_fake_without_credentials() -> None:
    assert isinstance(build_model(Settings()), FakeChatModel)
    partial = Settings(model_url="https://api.example.com", model_name="glm-4")
    assert isinstance(build_model(partial), FakeChatModel)


def test_build_model_selects_openai_compatible_family() -> None:
    for provider in ("openai", "openai_compatible", "glm", "zhipu", "deepseek", "qwen", "ollama", "vllm"):
        settings = Settings(
            model_url="https://api.example.com/v1",
            model_name="glm-4",
            model_api_key="secret",
            model_provider=provider,
        )
        model = build_model(settings)
        assert isinstance(model, OpenAICompatibleModel)
        assert model.provider == provider.replace("_", "-")


def test_build_model_selects_anthropic_and_strips_version_suffix() -> None:
    settings = Settings(
        model_url="https://api.anthropic.com/v1",
        model_name="claude-3",
        model_api_key="secret",
        model_provider="anthropic",
    )
    model = build_model(settings)
    assert isinstance(model, OpenAICompatibleModel)
    assert model.provider == "anthropic"
    assert model.base_url == "https://api.anthropic.com"  # /v1 后缀被移除


def test_build_model_rejects_unknown_provider() -> None:
    settings = Settings(
        model_url="https://api.example.com",
        model_name="m",
        model_api_key="secret",
        model_provider="unknown",
    )
    with pytest.raises(ValueError, match="unsupported model provider"):
        build_model(settings)
