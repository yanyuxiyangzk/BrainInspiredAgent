"""Codex-style interactive model selection for the BIA shell.

``/model`` renders numbered option lists (``> 1. name (current)`` with an
aligned description column) that support arrow keys, number shortcuts,
Enter and Esc, mirroring the model picker of the Codex CLI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from active_agent_platform.foundation import Settings

PICKER_STYLE = Style.from_dict({
    "picker-title": "bold",
    "picker-hover": "bold #38bdf8",
    "picker-current": "#38bdf8",
    "picker-current-description": "#e2e8f0",
    "picker-description": "#7a828a",
    "picker-footer": "#7a828a",
})
PICKER_FOOTER = "↑/↓ 选择 · 数字直选 · 回车确认 · Esc 取消"
OLLAMA_PROVIDER = "ollama"


@dataclass(frozen=True, slots=True)
class ModelOption:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    label: str
    default_url: str
    summary: str
    models: tuple[ModelOption, ...]


PROVIDER_CATALOG: tuple[ProviderEntry, ...] = (
    ProviderEntry("glm", "https://open.bigmodel.cn/api/paas/v4", "智谱 BigModel · open.bigmodel.cn", (
        ModelOption("glm-5.3-flash", "最新高速款，日常对话与摘要"),
        ModelOption("glm-5.3", "最新旗舰，复杂推理与规划"),
        ModelOption("glm-4-flash", "上一代免费款，保底可用"),
    )),
    ProviderEntry("deepseek", "https://api.deepseek.com/v1", "DeepSeek 官方 · api.deepseek.com", (
        ModelOption("deepseek-chat", "通用对话与摘要，响应快"),
        ModelOption("deepseek-reasoner", "深度推理，适合复杂分析"),
    )),
    ProviderEntry("openai", "https://api.openai.com/v1", "OpenAI 官方 · api.openai.com", (
        ModelOption("gpt-4o-mini", "轻量快速，成本低"),
        ModelOption("gpt-4o", "多模态旗舰，综合能力强"),
        ModelOption("o3-mini", "推理型小模型，擅长逻辑任务"),
    )),
    ProviderEntry("anthropic", "https://api.anthropic.com", "Anthropic 官方 · api.anthropic.com", (
        ModelOption("claude-3-5-sonnet-latest", "均衡的编码与分析模型"),
        ModelOption("claude-3-7-sonnet-latest", "增强推理的最新 Sonnet"),
    )),
    ProviderEntry(
        "qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "阿里云百炼 · dashscope.aliyuncs.com",
        (
            ModelOption("qwen-plus", "能力与成本均衡"),
            ModelOption("qwen-max", "能力最强的通义模型"),
        ),
    ),
    ProviderEntry("ollama", "http://localhost:11434/v1", "本地推理 · localhost:11434", (
        ModelOption("llama3.2", "Meta 开源轻量模型，本地运行"),
        ModelOption("qwen2.5", "通义开源模型，本地运行"),
    )),
)


def resolve_provider_index(provider: str) -> int | None:
    return next(
        (index for index, entry in enumerate(PROVIDER_CATALOG) if entry.label == provider), None,
    )


def resolve_model_index(entry: ProviderEntry, model_name: str) -> int | None:
    return next((index for index, item in enumerate(entry.models) if item.name == model_name), None)


def picker_fragments(
    title: str,
    options: Sequence[tuple[str, str]],
    hovered: int,
    current: int | None = None,
    footer: str = PICKER_FOOTER,
) -> StyleAndTextTuples:
    """Build the styled rows: hovered row gets ``>``, current row cyan ``(current)``."""
    fragments: StyleAndTextTuples = [("class:picker-title", f"{title}\n")]
    left_parts = [
        f"{index + 1}. {name}" + (" (current)" if index == current else "")
        for index, (name, _) in enumerate(options)
    ]
    width = max((len(part) for part in left_parts), default=0)
    for index, (_, description) in enumerate(options):
        is_hovered, is_current = index == hovered, index == current
        prefix = "> " if is_hovered else "  "
        name_style = "class:picker-current" if is_current else "class:picker-hover" if is_hovered else ""
        fragments.append((name_style, prefix + left_parts[index].ljust(width)))
        description_style = "class:picker-current-description" if is_current else "class:picker-description"
        fragments.append((description_style, "  " + description + "\n"))
    fragments.append(("class:picker-footer", footer + "\n"))
    return fragments


def _picker_application(
    title: str, options: Sequence[tuple[str, str]], current: int | None,
) -> Application[int | None]:
    state = {"hovered": current if current is not None else 0}
    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("c-p")
    def _move_up(event: object) -> None:  # pragma: no cover - prompt-toolkit callbacks
        state["hovered"] = (state["hovered"] - 1) % len(options)

    @bindings.add("down")
    @bindings.add("c-n")
    def _move_down(event: object) -> None:  # pragma: no cover - prompt-toolkit callbacks
        state["hovered"] = (state["hovered"] + 1) % len(options)

    for digit in "123456789":
        @bindings.add(digit)
        def _jump(event: object, digit: str = digit) -> None:  # pragma: no cover - prompt-toolkit
            index = int(digit) - 1
            if index < len(options):
                event.app.exit(index)  # type: ignore[attr-defined]

    @bindings.add("enter")
    def _confirm(event: object) -> None:  # pragma: no cover - prompt-toolkit callbacks
        event.app.exit(state["hovered"])  # type: ignore[attr-defined]

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(event: object) -> None:  # pragma: no cover - prompt-toolkit callbacks
        event.app.exit(None)  # type: ignore[attr-defined]

    def _render() -> StyleAndTextTuples:  # pragma: no cover - prompt-toolkit callbacks
        return picker_fragments(title, options, state["hovered"], current)

    application: Application[int | None] = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(_render))])),
        key_bindings=bindings, style=PICKER_STYLE, full_screen=False,
    )
    return application


async def pick_index(
    title: str, options: Sequence[tuple[str, str]], current: int | None = None,
) -> int | None:  # pragma: no cover - requires a live TTY
    application: Application[int | None] = _picker_application(title, options, current)
    return await application.run_async()


def save_model_env(values: Mapping[str, str], env_path: Path) -> Path:
    """Merge model settings into env_path, preserving unrelated lines."""
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing:
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in values:
            output.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            output.append(line)
    output.extend(f"{name}={value}" for name, value in values.items() if name not in seen)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return env_path


@dataclass(frozen=True, slots=True)
class _Selection:
    provider: str
    default_url: str
    model: str


def _status_line(settings: Settings) -> str:
    key = "已配置 ✓" if settings.model_api_key else "未配置"
    return (f"当前模型: {settings.model_provider or '未配置'} · {settings.model_name or '未配置'}"
            f" · Endpoint {settings.model_url or '未配置'} · API Key {key}")


async def _select_interactive(settings: Settings) -> _Selection | None:
    provider_choice = await pick_index(
        "选择 Provider", [(entry.label, entry.summary) for entry in PROVIDER_CATALOG],
        resolve_provider_index(settings.model_provider),
    )
    if provider_choice is None:
        return None
    entry = PROVIDER_CATALOG[provider_choice]
    model_choice = await pick_index(
        f"选择模型 ({entry.label})", [(item.name, item.description) for item in entry.models],
        resolve_model_index(entry, settings.model_name),
    )
    if model_choice is None:
        return None
    return _Selection(entry.label, entry.default_url, entry.models[model_choice].name)


async def _select_fallback(
    settings: Settings, ask: Callable[[str], Awaitable[str]], stdout: TextIO,
) -> _Selection:
    stdout.write("选择 Provider\n")
    stdout.writelines(
        f"  {index}. {entry.label}  {entry.summary}\n"
        for index, entry in enumerate(PROVIDER_CATALOG, 1)
    )
    choice = await ask(f"Provider [{settings.model_provider}]: ")
    entry: ProviderEntry | None = None
    provider = settings.model_provider
    default_url = settings.model_url
    if choice.isdigit() and 1 <= int(choice) <= len(PROVIDER_CATALOG):
        entry = PROVIDER_CATALOG[int(choice) - 1]
        provider, default_url = entry.label, entry.default_url
    elif choice:
        provider, default_url = choice, ""
    elif (current := resolve_provider_index(provider)) is not None:
        entry = PROVIDER_CATALOG[current]
        default_url = entry.default_url
    if entry is None:
        model = (await ask(f"Model [{settings.model_name}]: ")) or settings.model_name
        return _Selection(provider, default_url, model)
    stdout.write(f"选择模型 ({entry.label})\n")
    stdout.writelines(
        f"  {index}. {item.name}  {item.description}\n"
        for index, item in enumerate(entry.models, 1)
    )
    model_choice = await ask(f"Model [{settings.model_name or entry.models[0].name}]: ")
    if model_choice.isdigit() and 1 <= int(model_choice) <= len(entry.models):
        model = entry.models[int(model_choice) - 1].name
    else:
        model = model_choice or settings.model_name or entry.models[0].name
    return _Selection(provider, default_url, model)


async def configure_model(
    settings: Settings,
    *,
    ask: Callable[[str], Awaitable[str]],
    ask_secret: Callable[[str], Awaitable[str]],
    stdout: TextIO,
    stderr: TextIO,
    interactive: bool,
    env_path: Path | None = None,
) -> _Selection | None:
    """Run the /model flow: show status, pick provider and model, persist to .env.

    Returns the saved selection, or ``None`` when the user cancelled.
    """
    stdout.write(_status_line(settings) + "\n")
    if interactive:
        selection = await _select_interactive(settings)
    else:
        selection = await _select_fallback(settings, ask, stdout)
    if selection is None:
        stderr.write("模型配置已取消，未做修改。\n")
        return None
    keep_url = selection.provider == settings.model_provider and settings.model_url
    default_url = settings.model_url if keep_url else selection.default_url
    url = (await ask(f"Endpoint [{default_url}]: ")) or default_url
    key = (await ask_secret("API Key [留空保持不变]: ")) or settings.model_api_key
    if not url or not selection.model or (not key and selection.provider != OLLAMA_PROVIDER):
        stderr.write("模型配置已取消：Endpoint、模型名称与 API Key 均必填（ollama 除外）。\n")
        return None
    path = save_model_env({
        "BIA_MODEL_PROVIDER": selection.provider,
        "BIA_MODEL_URL": url,
        "BIA_MODEL_NAME": selection.model,
        "BIA_MODEL_API_KEY": key,
    }, env_path or Path.cwd() / ".env")
    stdout.write(f"✓ 模型配置已保存到 {path}，重启 bia 后生效。\n")
    return selection
