from decimal import Decimal

from agent_eval.db.enums import PrivateAgentPreset
from agent_eval.scoring.pricing import (
    MODEL_PRICING,
    extract_template_model,
    find_model_pricing,
    resolve_agent_pricing,
    usage_cost_usd,
)


def test_private_preset_model_resolves_to_provider_pricing() -> None:
    pricing = resolve_agent_pricing(
        private_preset=PrivateAgentPreset.DEEPSEEK,
        private_model="deepseek chat",
        request_template="{}",
    )

    assert pricing is not None
    assert pricing == MODEL_PRICING["DeepSeek"]["deepseek-v4-flash"]


def test_private_preset_unknown_model_has_no_pricing() -> None:
    pricing = resolve_agent_pricing(
        private_preset=PrivateAgentPreset.MIMO,
        private_model="mimo v2.5 pro ultraspeed",
        request_template="{}",
    )

    assert pricing is None


def test_public_agent_template_model_matches_across_providers() -> None:
    template = '{"model": "GPT-4o-mini", "messages": [{"role": "user", "content": "{{prompt}}"}]}'

    pricing = resolve_agent_pricing(
        private_preset=None,
        private_model=None,
        request_template=template,
    )

    assert pricing is not None
    assert pricing == MODEL_PRICING["OpenAI"]["gpt-4o-mini"]


def test_public_agent_unparseable_or_unknown_model_has_no_pricing() -> None:
    assert extract_template_model("not json {{prompt}}") is None
    assert extract_template_model('{"messages": []}') is None
    assert extract_template_model('{"model": 42}') is None
    assert (
        resolve_agent_pricing(
            private_preset=None,
            private_model=None,
            request_template='{"model": "acme-secret-model-9000"}',
        )
        is None
    )


def test_find_model_pricing_prefers_matching_provider() -> None:
    assert find_model_pricing("glm-5.2", "Zhipu") == MODEL_PRICING["Zhipu"]["glm-5.2"]
    assert find_model_pricing("  GLM-5.2  ", "Zhipu") == MODEL_PRICING["Zhipu"]["glm-5.2"]
    assert find_model_pricing("glm-5.2", "OpenAI") == MODEL_PRICING["Zhipu"]["glm-5.2"]
    assert find_model_pricing("gpt-5.6", "OpenAI") is None
    assert find_model_pricing("") is None


def test_usage_cost_usd_scales_per_million_tokens() -> None:
    pricing = MODEL_PRICING["Anthropic"]["claude-sonnet-5"]

    assert usage_cost_usd(pricing, 1_000_000, 500_000) == Decimal("7")
    assert usage_cost_usd(pricing, 0, 0) == Decimal("0")
