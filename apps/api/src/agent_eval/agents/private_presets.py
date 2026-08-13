import json
from dataclasses import dataclass, field

from agent_eval.db.enums import PrivateAgentPreset


@dataclass(frozen=True)
class PrivatePresetConfig:
    endpoint_url: str
    provider: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    api_key_header: str | None = None


PRIVATE_AGENT_PRESETS: dict[PrivateAgentPreset, PrivatePresetConfig] = {
    PrivateAgentPreset.DASHSCOPE: PrivatePresetConfig(
        endpoint_url=("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
        provider="DashScope",
    ),
    PrivateAgentPreset.DEEPSEEK: PrivatePresetConfig(
        endpoint_url="https://api.deepseek.com/chat/completions",
        provider="DeepSeek",
    ),
    PrivateAgentPreset.OPENAI: PrivatePresetConfig(
        endpoint_url="https://api.openai.com/v1/chat/completions",
        provider="OpenAI",
    ),
    PrivateAgentPreset.ZHIPU: PrivatePresetConfig(
        endpoint_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        provider="Zhipu",
    ),
    PrivateAgentPreset.GROK: PrivatePresetConfig(
        endpoint_url="https://api.x.ai/v1/chat/completions",
        provider="xAI",
    ),
    PrivateAgentPreset.CLAUDE: PrivatePresetConfig(
        endpoint_url="https://api.anthropic.com/v1/chat/completions",
        provider="Anthropic",
        extra_headers={"anthropic-version": "2023-06-01"},
        api_key_header="x-api-key",
    ),
    PrivateAgentPreset.MIMO: PrivatePresetConfig(
        endpoint_url="https://api.xiaomimimo.com/v1/chat/completions",
        provider="Xiaomi MiMo",
    ),
}

PRIVATE_MODEL_ALIASES: dict[PrivateAgentPreset, dict[str, str]] = {
    PrivateAgentPreset.DASHSCOPE: {
        "qwen3.8 max": "qwen3.8-max",
        "qwen 3.8 max": "qwen3.8-max",
        "qwen 3.7 plus": "qwen3.7-plus",
        "qwen3.7 flash": "qwen3.7-flash",
        "qwen plus": "qwen-plus",
        "qwen max": "qwen-max",
        "qwen math turbo": "qwen-math-turbo",
        "qwen3.7 plus": "qwen3.7-plus",
    },
    PrivateAgentPreset.DEEPSEEK: {
        "deepseek v4 pro": "deepseek-v4-pro",
        "deepseek v4 flash": "deepseek-v4-flash",
        # Retired on 2026-07-24; keep aliases for user input compatibility.
        "deepseek chat": "deepseek-v4-flash",
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek reasoner": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-flash",
    },
    PrivateAgentPreset.OPENAI: {
        "gpt 5.6": "gpt-5.6",
        "gpt 5.6 sol": "gpt-5.6-sol",
        "gpt 5.6 terra": "gpt-5.6-terra",
        "gpt 5.6 luna": "gpt-5.6-luna",
        "gpt 4.1": "gpt-4.1",
        "gpt 4.1 mini": "gpt-4.1-mini",
        "gpt 4o": "gpt-4o",
        "gpt 4o mini": "gpt-4o-mini",
    },
    PrivateAgentPreset.ZHIPU: {
        "glm 5.2": "glm-5.2",
        "glm 5.1": "glm-5.1",
        "glm 5": "glm-5",
        "glm 5 turbo": "glm-5-turbo",
        "glm 4.7": "glm-4.7",
        "glm 4.7 flash": "glm-4.7-flash",
        "glm 4.5": "glm-4.5",
        "glm 4.5 flash": "glm-4.5-flash",
    },
    PrivateAgentPreset.GROK: {
        "grok 4.6": "grok-4.6",
        "grok 4.5": "grok-4.5",
        "grok 4.3": "grok-4.3",
        "grok 4": "grok-4.6",
    },
    PrivateAgentPreset.CLAUDE: {
        "claude opus 5": "claude-opus-5",
        "claude sonnet 5": "claude-sonnet-5",
        "claude fable 5": "claude-fable-5",
        "claude haiku 4 5": "claude-haiku-4-5",
        "claude haiku 4.5": "claude-haiku-4-5",
        "claude sonnet 4 5": "claude-sonnet-5",
        "claude sonnet 4.5": "claude-sonnet-5",
        "claude opus 4 6": "claude-opus-5",
        "claude opus 4.6": "claude-opus-5",
    },
    PrivateAgentPreset.MIMO: {
        "mimo v2.5 pro": "mimo-v2.5-pro",
        "mimo v2.5": "mimo-v2.5",
        "mimo 2.5 pro": "mimo-v2.5-pro",
        "mimo 2.5": "mimo-v2.5",
        "mimo v2.5 pro ultraspeed": "mimo-v2.5-pro-ultraspeed",
    },
}


def normalize_private_model(preset: PrivateAgentPreset, model: str) -> str:
    normalized = " ".join(model.strip().split())
    aliases = PRIVATE_MODEL_ALIASES[preset]
    folded = normalized.casefold()
    if folded in aliases:
        return aliases[folded]
    spaced = folded.replace("-", " ")
    return aliases.get(spaced, normalized)


def private_request_template(model: str) -> str:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "{{prompt}}"}],
            "stream": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def private_preset_request_options(
    preset: PrivateAgentPreset | None,
) -> tuple[dict[str, str], str | None]:
    if preset is None:
        return {}, None
    config = PRIVATE_AGENT_PRESETS[preset]
    return dict(config.extra_headers), config.api_key_header
