"""Static token price table used to convert recorded usage into USD cost.

Prices are USD per 1M tokens, queried on 2026-09-22 from each provider's
official pricing page (or a mirror that states it is verified against it):

- DashScope: https://help.aliyun.com/zh/model-studio/model-pricing
  Prices are listed in CNY per 1M tokens (华北2/全球 deployment, lowest
  context tier, 非思考 output where tiers differ) and converted at
  CNY_PER_USD below. Context-cache hits bill at 10% of the input rate.
- DeepSeek: https://api-docs.deepseek.com/quick_start/pricing via
  https://deepseek.ai/pricing (verified 2026-09-18). Off-peak rates; peak
  hours (01:00-04:00 and 06:00-10:00 UTC, Mon-Fri) bill at 2x.
- OpenAI: https://openai.com/api/pricing via
  https://benchlm.ai/openai/api-pricing (synced 2026-09-21). Cached input
  bills at 10% of the standard input rate. No official list price exists
  for a plain `gpt-5.6` tier, so it is deliberately absent.
- Zhipu: https://docs.z.ai/guides/overview/pricing (2026-09-18).
  glm-4.7-flash / glm-4.5-flash are listed as Free (recorded as 0).
- xAI: https://docs.x.ai/docs/models via
  https://benchlm.ai/xai/api-pricing (2026-09-20).
- Anthropic: https://platform.claude.com/docs/en/models/overview and
  https://aicost.tech/claude-api-pricing.html (verified 2026-08). Cache
  reads bill at 10% of the input rate.
- Xiaomi MiMo: https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go
  (2026-08-06, 海外定价 in USD). mimo-v2.5-pro-ultraspeed is not on the
  official rate card, so it is deliberately absent.

Recorded usage does not distinguish cache-hit tokens, so cost estimates
always apply the standard input rate; cached prices are reference data only.
Models without a verified price stay absent from the table and yield a
None cost instead of a guessed number.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from agent_eval.agents.private_presets import (
    PRIVATE_AGENT_PRESETS,
    normalize_private_model,
)
from agent_eval.db.enums import PrivateAgentPreset

CNY_PER_USD = Decimal("7.2")
TOKENS_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None


def _cny(input_yuan: str, output_yuan: str, *, cached: bool = False) -> ModelPricing:
    input_usd = Decimal(input_yuan) / CNY_PER_USD
    return ModelPricing(
        input_per_mtok=input_usd,
        output_per_mtok=Decimal(output_yuan) / CNY_PER_USD,
        cached_input_per_mtok=input_usd / 10 if cached else None,
    )


MODEL_PRICING: dict[str, dict[str, ModelPricing]] = {
    "DashScope": {
        "qwen3.8-max": _cny("12", "36", cached=True),
        "qwen3.7-plus": _cny("2", "8", cached=True),
        "qwen3.7-flash": _cny("0.2", "0.8", cached=True),
        "qwen-plus": _cny("0.8", "2"),
        "qwen-max": _cny("2.4", "9.6"),
        "qwen-math-turbo": _cny("2", "6"),
    },
    "DeepSeek": {
        "deepseek-v4-pro": ModelPricing(
            input_per_mtok=Decimal("0.66"),
            output_per_mtok=Decimal("1.98"),
            cached_input_per_mtok=Decimal("0.022"),
        ),
        "deepseek-v4-flash": ModelPricing(
            input_per_mtok=Decimal("0.15"),
            output_per_mtok=Decimal("0.60"),
            cached_input_per_mtok=Decimal("0.003"),
        ),
        "deepseek-flash": ModelPricing(
            input_per_mtok=Decimal("0.15"),
            output_per_mtok=Decimal("0.60"),
            cached_input_per_mtok=Decimal("0.003"),
        ),
    },
    "OpenAI": {
        "gpt-5.6-sol": ModelPricing(
            input_per_mtok=Decimal("5"),
            output_per_mtok=Decimal("30"),
            cached_input_per_mtok=Decimal("0.5"),
        ),
        "gpt-5.6-terra": ModelPricing(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("12"),
            cached_input_per_mtok=Decimal("0.2"),
        ),
        "gpt-5.6-luna": ModelPricing(
            input_per_mtok=Decimal("0.20"),
            output_per_mtok=Decimal("1.20"),
            cached_input_per_mtok=Decimal("0.02"),
        ),
        "gpt-4.1": ModelPricing(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("8"),
            cached_input_per_mtok=Decimal("0.5"),
        ),
        "gpt-4.1-mini": ModelPricing(
            input_per_mtok=Decimal("0.40"),
            output_per_mtok=Decimal("1.60"),
            cached_input_per_mtok=Decimal("0.10"),
        ),
        "gpt-4o": ModelPricing(
            input_per_mtok=Decimal("2.50"),
            output_per_mtok=Decimal("10"),
            cached_input_per_mtok=Decimal("1.25"),
        ),
        "gpt-4o-mini": ModelPricing(
            input_per_mtok=Decimal("0.15"),
            output_per_mtok=Decimal("0.60"),
            cached_input_per_mtok=Decimal("0.075"),
        ),
    },
    "Zhipu": {
        "glm-5.2": ModelPricing(
            input_per_mtok=Decimal("1.4"),
            output_per_mtok=Decimal("4.4"),
            cached_input_per_mtok=Decimal("0.26"),
        ),
        "glm-5.1": ModelPricing(
            input_per_mtok=Decimal("1.4"),
            output_per_mtok=Decimal("4.4"),
            cached_input_per_mtok=Decimal("0.26"),
        ),
        "glm-5": ModelPricing(
            input_per_mtok=Decimal("1"),
            output_per_mtok=Decimal("3.2"),
            cached_input_per_mtok=Decimal("0.2"),
        ),
        "glm-4.7": ModelPricing(
            input_per_mtok=Decimal("0.6"),
            output_per_mtok=Decimal("2.2"),
            cached_input_per_mtok=Decimal("0.11"),
        ),
        "glm-4.7-flash": ModelPricing(
            input_per_mtok=Decimal("0"),
            output_per_mtok=Decimal("0"),
            cached_input_per_mtok=Decimal("0"),
        ),
        "glm-4.5": ModelPricing(
            input_per_mtok=Decimal("0.6"),
            output_per_mtok=Decimal("2.2"),
            cached_input_per_mtok=Decimal("0.11"),
        ),
        "glm-4.5-flash": ModelPricing(
            input_per_mtok=Decimal("0"),
            output_per_mtok=Decimal("0"),
            cached_input_per_mtok=Decimal("0"),
        ),
    },
    "xAI": {
        "grok-4.6": ModelPricing(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("6"),
            cached_input_per_mtok=Decimal("0.5"),
        ),
        "grok-4.5": ModelPricing(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("6"),
            cached_input_per_mtok=Decimal("0.3"),
        ),
        "grok-4.3": ModelPricing(
            input_per_mtok=Decimal("1.25"),
            output_per_mtok=Decimal("2.50"),
        ),
    },
    "Anthropic": {
        "claude-fable-5": ModelPricing(
            input_per_mtok=Decimal("10"),
            output_per_mtok=Decimal("50"),
            cached_input_per_mtok=Decimal("1"),
        ),
        "claude-opus-5": ModelPricing(
            input_per_mtok=Decimal("5"),
            output_per_mtok=Decimal("25"),
            cached_input_per_mtok=Decimal("0.5"),
        ),
        "claude-sonnet-5": ModelPricing(
            input_per_mtok=Decimal("2"),
            output_per_mtok=Decimal("10"),
            cached_input_per_mtok=Decimal("0.2"),
        ),
        "claude-haiku-4-5": ModelPricing(
            input_per_mtok=Decimal("1"),
            output_per_mtok=Decimal("5"),
            cached_input_per_mtok=Decimal("0.1"),
        ),
    },
    "Xiaomi MiMo": {
        "mimo-v2.6-pro": ModelPricing(
            input_per_mtok=Decimal("0.435"),
            output_per_mtok=Decimal("0.87"),
            cached_input_per_mtok=Decimal("0.0036"),
        ),
        "mimo-v2.6-flash": ModelPricing(
            input_per_mtok=Decimal("0.14"),
            output_per_mtok=Decimal("0.28"),
            cached_input_per_mtok=Decimal("0.0028"),
        ),
        "mimo-v2.6-pro-ultraspeed": ModelPricing(
            input_per_mtok=Decimal("4.35"),
            output_per_mtok=Decimal("8.7"),
            cached_input_per_mtok=Decimal("0.036"),
        ),
        "mimo-v2.5-pro": ModelPricing(
            input_per_mtok=Decimal("0.435"),
            output_per_mtok=Decimal("0.87"),
            cached_input_per_mtok=Decimal("0.0036"),
        ),
        "mimo-v2.5": ModelPricing(
            input_per_mtok=Decimal("0.14"),
            output_per_mtok=Decimal("0.28"),
            cached_input_per_mtok=Decimal("0.0028"),
        ),
    },
}


def find_model_pricing(model: str, provider: str | None = None) -> ModelPricing | None:
    key = " ".join(model.strip().split()).casefold()
    if not key:
        return None
    tables = MODEL_PRICING
    if provider is not None and provider in tables:
        pricing = tables[provider].get(key)
        if pricing is not None:
            return pricing
    for table_provider in sorted(tables):
        if table_provider == provider:
            continue
        pricing = tables[table_provider].get(key)
        if pricing is not None:
            return pricing
    return None


def extract_template_model(request_template: str) -> str | None:
    try:
        payload = json.loads(request_template)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def resolve_agent_pricing(
    *,
    private_preset: PrivateAgentPreset | None,
    private_model: str | None,
    request_template: str,
) -> ModelPricing | None:
    if private_preset is not None and private_model is not None:
        provider = PRIVATE_AGENT_PRESETS[private_preset].provider
        model = normalize_private_model(private_preset, private_model)
        return find_model_pricing(model, provider)
    template_model = extract_template_model(request_template)
    if template_model is None:
        return None
    return find_model_pricing(template_model)


def usage_cost_usd(
    pricing: ModelPricing, input_tokens: int, output_tokens: int
) -> Decimal:
    return (
        Decimal(input_tokens) * pricing.input_per_mtok
        + Decimal(output_tokens) * pricing.output_per_mtok
    ) / TOKENS_PER_MILLION
