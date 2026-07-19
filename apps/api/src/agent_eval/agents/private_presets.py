import json
from dataclasses import dataclass

from agent_eval.db.enums import PrivateAgentPreset


@dataclass(frozen=True)
class PrivatePresetConfig:
    endpoint_url: str
    provider: str


PRIVATE_AGENT_PRESETS: dict[PrivateAgentPreset, PrivatePresetConfig] = {
    PrivateAgentPreset.DASHSCOPE: PrivatePresetConfig(
        endpoint_url=(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        ),
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
}

PRIVATE_MODEL_ALIASES: dict[PrivateAgentPreset, dict[str, str]] = {
    PrivateAgentPreset.DASHSCOPE: {
        "qwen plus": "qwen-plus",
        "qwen max": "qwen-max",
        "qwen math turbo": "qwen-math-turbo",
        "qwen3.7 plus": "qwen3.7-plus",
    },
    PrivateAgentPreset.DEEPSEEK: {
        "deepseek v4 pro": "deepseek-v4-pro",
        "deepseek v4 flash": "deepseek-v4-flash",
        "deepseek chat": "deepseek-chat",
        "deepseek reasoner": "deepseek-reasoner",
    },
    PrivateAgentPreset.OPENAI: {
        "gpt 4.1": "gpt-4.1",
        "gpt 4.1 mini": "gpt-4.1-mini",
        "gpt 4o": "gpt-4o",
        "gpt 4o mini": "gpt-4o-mini",
    },
}


def normalize_private_model(preset: PrivateAgentPreset, model: str) -> str:
    normalized = " ".join(model.strip().split())
    return PRIVATE_MODEL_ALIASES[preset].get(normalized.casefold(), normalized)


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
