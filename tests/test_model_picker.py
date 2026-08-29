from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from active_agent_platform.foundation import Settings
from apps.quant_agent import model_picker
from apps.quant_agent.model_picker import (
    PROVIDER_CATALOG,
    configure_model,
    picker_fragments,
    resolve_model_index,
    resolve_provider_index,
    save_model_env,
)


def _ask_factory(responses: list[str]) -> object:
    async def ask(prompt: str) -> str:
        return responses.pop(0) if responses else ""

    return ask


def _ask_secret_factory(responses: list[str]) -> object:
    async def ask_secret(prompt: str) -> str:
        return responses.pop(0) if responses else ""

    return ask_secret


def test_provider_catalog_is_well_formed() -> None:
    labels = [entry.label for entry in PROVIDER_CATALOG]
    assert len(labels) == len(set(labels)) and "glm" in labels and "ollama" in labels
    for entry in PROVIDER_CATALOG:
        assert entry.default_url.startswith(("https://", "http://")) and entry.summary
        names = [item.name for item in entry.models]
        assert names and len(names) == len(set(names))
        assert all(item.description for item in entry.models)


def test_resolve_indexes_match_catalog() -> None:
    assert resolve_provider_index("glm") == 0
    assert resolve_provider_index("openai-compatible") is None
    assert resolve_provider_index("missing") is None
    entry = PROVIDER_CATALOG[0]
    assert resolve_model_index(entry, entry.models[0].name) == 0
    assert resolve_model_index(entry, "other") is None


def test_picker_fragments_mark_current_and_hover() -> None:
    options = (("glm-4-flash", "免费高速"), ("glm-4-plus", "旗舰模型"))
    fragments = picker_fragments("选择模型 (glm)", options, hovered=1, current=0)
    text = "".join(value for _, value in fragments)
    assert text.startswith("选择模型 (glm)\n") and "(current)" in text and "> " in text
    assert text.endswith("↑/↓ 选择 · 数字直选 · 回车确认 · Esc 取消\n")
    current_rows = [value for style, value in fragments if style == "class:picker-current"]
    hover_rows = [value for style, value in fragments if style == "class:picker-hover"]
    assert any("(current)" in value for value in current_rows)
    assert any(value.startswith("> ") for value in hover_rows)
    name_rows = [value for style, value in fragments
                 if style in {"", "class:picker-current", "class:picker-hover"}]
    assert len(name_rows) == 2 and len({len(row) for row in name_rows}) == 1


def test_picker_application_binds_keys_and_style() -> None:
    application = model_picker._picker_application(
        "选择 Provider", [("glm", "智谱"), ("openai", "OpenAI")], 0,
    )
    assert application.key_bindings is not None
    assert application.style is model_picker.PICKER_STYLE


def test_save_model_env_creates_file(tmp_path: Path) -> None:
    path = save_model_env(
        {"BIA_MODEL_PROVIDER": "glm", "BIA_MODEL_NAME": "glm-4-plus"}, tmp_path / ".env",
    )
    content = path.read_text(encoding="utf-8")
    assert "BIA_MODEL_PROVIDER=glm" in content and "BIA_MODEL_NAME=glm-4-plus" in content


def test_save_model_env_merges_and_preserves(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# bia config\nBIA_LOG_LEVEL=INFO\nBIA_MODEL_URL=https://old.example.com\n",
        encoding="utf-8",
    )
    save_model_env(
        {"BIA_MODEL_URL": "https://new.example.com", "BIA_MODEL_NAME": "glm-4-flash"}, env,
    )
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# bia config" and "BIA_LOG_LEVEL=INFO" in lines
    assert "BIA_MODEL_URL=https://new.example.com" in lines
    assert "BIA_MODEL_NAME=glm-4-flash" in lines
    assert "https://old.example.com" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_configure_model_fallback_saves_selection(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    responses = ["1", "2", "", "sk-test"]
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=False, env_path=env_path,
    )
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["BIA_MODEL_PROVIDER"] == "glm"
    assert values["BIA_MODEL_NAME"] == "glm-4-plus"
    assert values["BIA_MODEL_URL"] == PROVIDER_CATALOG[0].default_url
    assert values["BIA_MODEL_API_KEY"] == "sk-test"
    assert "当前模型" in stdout.getvalue() and "✓" in stdout.getvalue()
    assert "选择 Provider" in stdout.getvalue() and "选择模型 (glm)" in stdout.getvalue()
    assert not stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_fallback_accepts_custom_provider(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    responses = ["my-provider", "my-model", "https://custom.example.com/v1", "sk-custom"]
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=False, env_path=env_path,
    )
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["BIA_MODEL_PROVIDER"] == "my-provider"
    assert values["BIA_MODEL_URL"] == "https://custom.example.com/v1"
    assert values["BIA_MODEL_NAME"] == "my-model"
    assert not stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_fallback_allows_empty_key_for_ollama(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    ollama_index = resolve_provider_index("ollama")
    assert ollama_index is not None
    responses = [str(ollama_index + 1), "1", "", ""]
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=False, env_path=env_path,
    )
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["BIA_MODEL_PROVIDER"] == "ollama" and values["BIA_MODEL_API_KEY"] == ""
    assert not stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_requires_api_key(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    responses = ["1", "1", "", ""]
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=False, env_path=env_path,
    )
    assert not env_path.exists()
    assert "API Key" in stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_interactive_uses_picker_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = iter([0, 1])

    async def fake_pick(title: str, options: object, current: int | None = None) -> int | None:
        del title, options, current
        return next(choices)

    monkeypatch.setattr(model_picker, "pick_index", fake_pick)
    stdout, stderr = StringIO(), StringIO()
    responses = ["", "sk-abc"]
    env_path = tmp_path / "chosen.env"
    await configure_model(
        Settings(), ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=True, env_path=env_path,
    )
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["BIA_MODEL_PROVIDER"] == "glm" and values["BIA_MODEL_NAME"] == "glm-4-plus"
    assert values["BIA_MODEL_API_KEY"] == "sk-abc"
    assert not stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_fallback_keeps_current_provider(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    existing = Settings(
        model_provider="glm",
        model_url=PROVIDER_CATALOG[0].default_url,
        model_name="glm-4-air",
        model_api_key="sk-keep",
    )
    responses = ["", "", "", ""]
    env_path = tmp_path / ".env"
    await configure_model(
        existing, ask=_ask_factory(responses), ask_secret=_ask_secret_factory(responses),
        stdout=stdout, stderr=stderr, interactive=False, env_path=env_path,
    )
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["BIA_MODEL_PROVIDER"] == "glm" and values["BIA_MODEL_NAME"] == "glm-4-air"
    assert values["BIA_MODEL_URL"] == PROVIDER_CATALOG[0].default_url
    assert values["BIA_MODEL_API_KEY"] == "sk-keep"
    assert not stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_interactive_cancel_at_model_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = iter([0, None])

    async def fake_pick(title: str, options: object, current: int | None = None) -> int | None:
        del title, options, current
        return next(choices)

    monkeypatch.setattr(model_picker, "pick_index", fake_pick)
    stdout, stderr = StringIO(), StringIO()
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory([]), ask_secret=_ask_secret_factory([]),
        stdout=stdout, stderr=stderr, interactive=True, env_path=env_path,
    )
    assert not env_path.exists()
    assert "已取消" in stderr.getvalue()


@pytest.mark.asyncio
async def test_configure_model_interactive_cancel_keeps_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_pick(title: str, options: object, current: int | None = None) -> int | None:
        del title, options, current
        return None

    monkeypatch.setattr(model_picker, "pick_index", fake_pick)
    stdout, stderr = StringIO(), StringIO()
    env_path = tmp_path / ".env"
    await configure_model(
        Settings(), ask=_ask_factory([]), ask_secret=_ask_secret_factory([]),
        stdout=stdout, stderr=stderr, interactive=True, env_path=env_path,
    )
    assert not env_path.exists()
    assert "已取消" in stderr.getvalue()
