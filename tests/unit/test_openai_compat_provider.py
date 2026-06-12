"""OpenAICompatProvider tests.

Covers:
- Conservative default capabilities: tool use yes; no caching, no
  image input, no document blocks; neutral price ratios; no cheap
  summariser model.
- The ``capabilities=`` override: a full :class:`Capabilities`
  instance is honored as-is; a dict applies field overrides on top
  of the conservative base; bad overrides fail loudly at
  construction.
- Constructor validation (``base_url`` / ``model`` required) and
  normalisation (trailing-slash strip).
- ``map_usage`` reads the OpenAI wire shape — the endpoints speak
  the Chat Completions protocol.
- Pricing: the ``("openai_compat", "*")`` wildcard row prices any
  model at $0 (estimate 0, not ``None``); an exact per-model row
  wins over the wildcard.
- A configured instance registers under ``openai_compat`` in the
  provider registry (it is never auto-seeded).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import (
    PRICING_ND_PER_TOKEN,
    PriceRow,
    estimate_nanodollars,
    estimate_per_category,
)
from inkfoot.providers import (
    Capabilities,
    OpenAICompatProvider,
    ProviderRegistry,
)


def _provider(**kwargs: Any) -> OpenAICompatProvider:
    defaults: dict[str, Any] = {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.2",
    }
    defaults.update(kwargs)
    return OpenAICompatProvider(**defaults)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    ProviderRegistry.clear()
    yield
    ProviderRegistry.clear()


# ----------------------------------------------------------------------
# Conservative default capabilities
# ----------------------------------------------------------------------


def test_default_capabilities_are_conservative() -> None:
    caps = _provider().get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_image_input is False
    assert caps.supports_document_block is False
    assert caps.supports_prompt_cache is False
    assert caps.prompt_cache_style == "none"
    assert caps.cache_read_price_ratio == pytest.approx(1.0)
    assert caps.cache_write_price_ratio == pytest.approx(1.0)
    assert caps.supports_response_format_json is False
    assert caps.cheap_model_for_summariser is None


def test_get_capabilities_ignores_the_model_argument() -> None:
    provider = _provider()
    assert (
        provider.get_capabilities(model="anything")
        == provider.get_capabilities()
    )


# ----------------------------------------------------------------------
# capabilities= override
# ----------------------------------------------------------------------


def test_full_capabilities_instance_is_honored_as_is() -> None:
    declared = Capabilities(
        supports_tool_use=True,
        supports_image_input=True,
        supports_document_block=False,
        supports_prompt_cache=True,
        prompt_cache_style="automatic",
        cache_read_price_ratio=0.5,
        cache_write_price_ratio=0.0,
        supports_response_format_json=True,
        cheap_model_for_summariser="llama3.2:1b",
    )
    provider = _provider(capabilities=declared)
    assert provider.get_capabilities() is declared


def test_dict_override_applies_on_top_of_the_conservative_base() -> None:
    provider = _provider(
        capabilities={"supports_response_format_json": True}
    )
    caps = provider.get_capabilities()
    assert caps.supports_response_format_json is True
    # Everything not overridden keeps the conservative default.
    assert caps.supports_prompt_cache is False
    assert caps.supports_tool_use is True


def test_dict_override_can_widen_caching() -> None:
    provider = _provider(
        capabilities={
            "supports_prompt_cache": True,
            "prompt_cache_style": "automatic",
            "cache_read_price_ratio": 0.5,
            "cache_write_price_ratio": 0.0,
        }
    )
    caps = provider.get_capabilities()
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "automatic"


def test_dict_override_with_unknown_field_fails_loudly() -> None:
    with pytest.raises(TypeError):
        _provider(capabilities={"supports_telepathy": True})


def test_incoherent_dict_override_fails_capabilities_validation() -> None:
    # Cache support without a cache style is the incoherent combo
    # Capabilities itself rejects — the override path must not
    # bypass that validation.
    with pytest.raises(ValueError, match="supports_prompt_cache=True"):
        _provider(capabilities={"supports_prompt_cache": True})


def test_capabilities_of_wrong_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="capabilities"):
        _provider(capabilities="automatic")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Constructor contract
# ----------------------------------------------------------------------


def test_requires_non_empty_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatProvider(base_url="", model="llama3.2")


def test_requires_non_empty_model() -> None:
    with pytest.raises(ValueError, match="model"):
        OpenAICompatProvider(
            base_url="http://localhost:11434/v1", model=""
        )


def test_base_url_trailing_slash_is_stripped() -> None:
    provider = _provider(base_url="http://localhost:11434/v1/")
    assert provider.base_url == "http://localhost:11434/v1"


def test_api_key_defaults_to_none_and_is_stored() -> None:
    assert _provider().api_key is None
    assert _provider(api_key="sk-local").api_key == "sk-local"


def test_declares_provider_type() -> None:
    provider = _provider()
    assert provider.PROVIDER_TYPE == "openai_compat"
    assert provider.model == "llama3.2"


# ----------------------------------------------------------------------
# map_usage — OpenAI wire shape
# ----------------------------------------------------------------------


def test_map_usage_reads_the_openai_wire_shape() -> None:
    usage = _provider().map_usage(
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 18,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 2},
            }
        }
    )
    assert usage.input_tokens == 120
    assert usage.output_tokens == 18
    assert usage.cache_read_tokens == 40
    assert usage.reasoning_tokens == 2
    assert usage.cache_status == "hit"


def test_map_usage_reads_attribute_style_objects() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=6)
    )
    usage = _provider().map_usage(response)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 6
    assert usage.cache_status == "n/a"


# ----------------------------------------------------------------------
# Wildcard pricing
# ----------------------------------------------------------------------


def _ledger() -> CausalTokenLedger:
    return CausalTokenLedger(user_input_tokens=1_000, output_tokens=200)


def test_any_model_prices_at_zero_via_the_wildcard_row() -> None:
    # Zero, not None — self-hosted is explicitly free, not unknown.
    assert estimate_nanodollars("openai_compat", "llama3.2", _ledger()) == 0
    assert (
        estimate_nanodollars(
            "openai_compat", "qwen2.5-coder:32b", _ledger()
        )
        == 0
    )


def test_per_category_estimate_is_all_zeros_via_the_wildcard_row() -> None:
    split = estimate_per_category("openai_compat", "llama3.2", _ledger())
    assert split
    assert all(value == 0 for value in split.values())


def test_exact_model_row_wins_over_the_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators on a paid compat endpoint add exact rows; the
    wildcard only catches models without one."""
    monkeypatch.setitem(
        PRICING_ND_PER_TOKEN,
        ("openai_compat", "llama3.2"),
        PriceRow(input=100, output=400, cache_read=100, cache_write=100),
    )
    estimate = estimate_nanodollars("openai_compat", "llama3.2", _ledger())
    assert estimate == 1_000 * 100 + 200 * 400
    # Other models still fall through to the $0 wildcard.
    assert estimate_nanodollars("openai_compat", "other", _ledger()) == 0


def test_provider_without_a_wildcard_row_still_returns_none() -> None:
    assert (
        estimate_nanodollars("anthropic", "claude-unknown", _ledger())
        is None
    )


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_configured_instance_registers_under_openai_compat() -> None:
    provider = _provider()
    ProviderRegistry.register(provider)
    assert ProviderRegistry.get("openai_compat") is provider


def test_capabilities_instances_are_immutable_declarations() -> None:
    caps = _provider().get_capabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.supports_prompt_cache = True  # type: ignore[misc]
