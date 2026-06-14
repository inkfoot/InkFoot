"""Provider abstraction unit tests.

Three layers under test:

* The declaration dataclasses — :class:`Capabilities` (validated
  cache-style coherence) and :class:`TokenUsage` (non-negative
  counters, validated cache status).
* The :class:`LLMProvider` contract, parametrised across every
  shipped provider (including an instance-configured
  ``OpenAICompatProvider``): non-empty type/model declarations, a
  capability declaration, and a ``map_usage`` that tolerates
  ``None`` / empty / garbage responses without raising.
* :data:`ProviderRegistry` — lazy seeding of the zero-config
  built-ins, name overrides, replace-with-warning semantics, and
  test-friendly ``clear()``.

``map_usage`` fixtures cover both the dict shape (test fixtures,
serialised replays) and the attribute-access shape (live SDK
objects) for each provider.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from inkfoot.providers import (
    AnthropicProvider,
    BedrockProvider,
    Capabilities,
    GeminiProvider,
    LLMProvider,
    OpenAICompatProvider,
    OpenAIProvider,
    ProviderRegistry,
    TokenUsage,
)
from inkfoot.providers.base import PROMPT_CACHE_STYLES

_BUILTIN_PROVIDERS = (
    AnthropicProvider(),
    OpenAIProvider(),
    GeminiProvider(),
    BedrockProvider(),
    OpenAICompatProvider(
        base_url="http://localhost:11434/v1", model="llama3.2"
    ),
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    ProviderRegistry.clear()
    yield
    ProviderRegistry.clear()


def _stub_capabilities() -> Capabilities:
    return Capabilities(
        supports_tool_use=False,
        supports_image_input=False,
        supports_document_block=False,
        supports_prompt_cache=False,
        prompt_cache_style="none",
        cache_read_price_ratio=1.0,
        cache_write_price_ratio=1.0,
        supports_response_format_json=False,
        cheap_model_for_summariser=None,
    )


class _StubProvider(LLMProvider):
    PROVIDER_TYPE = "stub"
    DEFAULT_MODEL = "stub-1"
    CAPABILITIES = _stub_capabilities()

    def map_usage(self, response: Any) -> TokenUsage:
        return TokenUsage()


# ----------------------------------------------------------------------
# Capabilities validation
# ----------------------------------------------------------------------


def test_capabilities_rejects_unknown_cache_style() -> None:
    with pytest.raises(ValueError, match="prompt_cache_style"):
        dataclasses.replace(
            _stub_capabilities(), prompt_cache_style="bogus"
        )


def test_capabilities_rejects_style_none_when_cache_supported() -> None:
    with pytest.raises(ValueError, match="supports_prompt_cache=True"):
        dataclasses.replace(
            _stub_capabilities(), supports_prompt_cache=True
        )


def test_capabilities_rejects_cache_style_without_cache_support() -> None:
    with pytest.raises(ValueError, match="requires "):
        dataclasses.replace(
            _stub_capabilities(), prompt_cache_style="automatic"
        )


def test_capabilities_rejects_negative_price_ratios() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        dataclasses.replace(
            _stub_capabilities(), cache_read_price_ratio=-0.1
        )


def test_capabilities_is_immutable() -> None:
    caps = _stub_capabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.supports_tool_use = True  # type: ignore[misc]


# ----------------------------------------------------------------------
# TokenUsage validation
# ----------------------------------------------------------------------


def test_token_usage_defaults_to_all_zeros_and_na() -> None:
    usage = TokenUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.cache_status == "n/a"


def test_token_usage_rejects_negative_counters() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TokenUsage(output_tokens=-1)


def test_token_usage_rejects_unknown_cache_status() -> None:
    with pytest.raises(ValueError, match="cache_status"):
        TokenUsage(cache_status="warm")


# ----------------------------------------------------------------------
# LLMProvider contract — every built-in provider
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider", _BUILTIN_PROVIDERS, ids=lambda p: p.PROVIDER_TYPE
)
def test_provider_declares_type_and_default_model(
    provider: LLMProvider,
) -> None:
    assert isinstance(provider.PROVIDER_TYPE, str)
    assert provider.PROVIDER_TYPE
    assert isinstance(provider.DEFAULT_MODEL, str)
    assert provider.DEFAULT_MODEL


@pytest.mark.parametrize(
    "provider", _BUILTIN_PROVIDERS, ids=lambda p: p.PROVIDER_TYPE
)
def test_provider_declares_capabilities(provider: LLMProvider) -> None:
    caps = provider.get_capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.prompt_cache_style in PROMPT_CACHE_STYLES
    # The model argument is accepted (per-model variance hook).
    assert provider.get_capabilities(model=provider.DEFAULT_MODEL) == caps


@pytest.mark.parametrize(
    "provider", _BUILTIN_PROVIDERS, ids=lambda p: p.PROVIDER_TYPE
)
def test_map_usage_of_none_response_is_all_zeros(
    provider: LLMProvider,
) -> None:
    assert provider.map_usage(None) == TokenUsage()


@pytest.mark.parametrize(
    "provider", _BUILTIN_PROVIDERS, ids=lambda p: p.PROVIDER_TYPE
)
@pytest.mark.parametrize(
    "response",
    [{}, {"usage": {}}, {"usage": None}, SimpleNamespace()],
    ids=["empty-dict", "empty-usage", "none-usage", "bare-object"],
)
def test_map_usage_of_empty_response_is_all_zeros(
    provider: LLMProvider, response: Any
) -> None:
    assert provider.map_usage(response) == TokenUsage()


@pytest.mark.parametrize(
    "provider", _BUILTIN_PROVIDERS, ids=lambda p: p.PROVIDER_TYPE
)
def test_map_usage_never_raises_on_garbage_counters(
    provider: LLMProvider,
) -> None:
    response = {
        "usage": {
            # Anthropic-shaped keys.
            "input_tokens": None,
            "output_tokens": "garbage",
            "cache_read_input_tokens": {"weird": 1},
            "cache_creation_input_tokens": -3,
            "thinking_tokens": "x",
            # OpenAI-shaped keys.
            "prompt_tokens": "x",
            "completion_tokens": [1],
            "prompt_tokens_details": "nope",
            "completion_tokens_details": 7,
        }
    }
    assert provider.map_usage(response) == TokenUsage()


# ----------------------------------------------------------------------
# Anthropic usage mapping
# ----------------------------------------------------------------------


def test_anthropic_usage_counters_map_to_token_usage() -> None:
    usage = AnthropicProvider().map_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 20,
            }
        }
    )
    # input_tokens is the full prompt: Anthropic's own field excludes
    # the cached portion, so the overlay adds it back.
    assert usage.input_tokens == 150
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 30
    assert usage.cache_creation_tokens == 20
    assert usage.cache_status == "partial"


def test_anthropic_sdk_object_usage_shape_is_supported() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=70,
            output_tokens=9,
            cache_read_input_tokens=30,
            cache_creation_input_tokens=0,
        )
    )
    usage = AnthropicProvider().map_usage(response)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 9
    assert usage.cache_read_tokens == 30
    assert usage.cache_status == "hit"


def test_anthropic_thinking_blocks_count_as_reasoning_tokens() -> None:
    usage = AnthropicProvider().map_usage(
        {
            "usage": {"output_tokens": 5},
            "content": [
                {"type": "thinking", "tokens": 17},
                {"type": "text", "text": "hi"},
            ],
        }
    )
    assert usage.reasoning_tokens == 17


def test_anthropic_usage_thinking_tokens_field_wins_over_blocks() -> None:
    usage = AnthropicProvider().map_usage(
        {
            "usage": {"output_tokens": 5, "thinking_tokens": 4},
            "content": [{"type": "thinking", "tokens": 17}],
        }
    )
    assert usage.reasoning_tokens == 4


def test_anthropic_cache_write_only_is_a_miss() -> None:
    usage = AnthropicProvider().map_usage(
        {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 40}}
    )
    assert usage.cache_status == "miss"


# ----------------------------------------------------------------------
# OpenAI usage mapping
# ----------------------------------------------------------------------


def test_openai_usage_counters_map_to_token_usage() -> None:
    usage = OpenAIProvider().map_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 60},
                "completion_tokens_details": {"reasoning_tokens": 3},
            }
        }
    )
    # OpenAI's prompt_tokens already includes the cached portion.
    assert usage.input_tokens == 100
    assert usage.output_tokens == 7
    assert usage.cache_read_tokens == 60
    assert usage.cache_creation_tokens == 0
    assert usage.reasoning_tokens == 3
    assert usage.cache_status == "hit"


def test_openai_sdk_object_usage_shape_is_supported() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=60),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        )
    )
    usage = OpenAIProvider().map_usage(response)
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 60
    assert usage.reasoning_tokens == 3
    assert usage.cache_status == "hit"


def test_openai_cache_creation_is_always_zero() -> None:
    usage = OpenAIProvider().map_usage(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 1}}
    )
    assert usage.cache_creation_tokens == 0
    assert usage.cache_status == "n/a"


# ----------------------------------------------------------------------
# Capability declarations
# ----------------------------------------------------------------------


def test_anthropic_capability_declaration() -> None:
    caps = AnthropicProvider().get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_document_block is True
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "explicit_marker"
    assert caps.cache_read_price_ratio == pytest.approx(0.1)
    assert caps.cache_write_price_ratio == pytest.approx(1.25)
    assert caps.supports_response_format_json is False
    assert caps.cheap_model_for_summariser == "claude-haiku-4-5"


def test_openai_capability_declaration() -> None:
    caps = OpenAIProvider().get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_document_block is False
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "automatic"
    assert caps.cache_read_price_ratio == pytest.approx(0.5)
    assert caps.cache_write_price_ratio == pytest.approx(0.0)
    assert caps.supports_response_format_json is True
    assert caps.cheap_model_for_summariser == "gpt-4o-mini"


def test_gemini_capability_declaration() -> None:
    caps = GeminiProvider().get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_image_input is True
    assert caps.supports_document_block is True
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "cache_resource"
    assert caps.cache_read_price_ratio == pytest.approx(0.25)
    assert caps.cache_write_price_ratio == pytest.approx(1.0)
    assert caps.supports_response_format_json is True
    assert caps.cheap_model_for_summariser == "gemini-1.5-flash"


def test_bedrock_capability_declaration() -> None:
    """Zero-arg capabilities resolve from the default model, which
    is in the Anthropic family — Claude on Bedrock keeps Anthropic's
    explicit-marker caching."""
    caps = BedrockProvider().get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_image_input is True
    assert caps.supports_document_block is True
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "explicit_marker"
    assert caps.cache_read_price_ratio == pytest.approx(0.1)
    assert caps.cache_write_price_ratio == pytest.approx(1.25)
    assert caps.supports_response_format_json is False
    assert caps.cheap_model_for_summariser == (
        "anthropic.claude-3-5-haiku-20241022-v1:0"
    )


def test_capability_ratios_match_the_pricing_table() -> None:
    """The declared cache price ratios aren't arbitrary — every row
    in the static pricing table for the provider derives its cache
    rates from them."""
    from inkfoot.pricing import PRICING_ND_PER_TOKEN

    for provider in _BUILTIN_PROVIDERS:
        caps = provider.get_capabilities()
        rows = [
            row
            for (ptype, _model), row in PRICING_ND_PER_TOKEN.items()
            if ptype == provider.PROVIDER_TYPE
        ]
        assert rows, f"no pricing rows for {provider.PROVIDER_TYPE}"
        for row in rows:
            assert row.cache_read == int(
                row.input * caps.cache_read_price_ratio
            )
            assert row.cache_write == int(
                row.input * caps.cache_write_price_ratio
            )


def test_provider_without_capabilities_must_override_accessor() -> None:
    class _NoCaps(LLMProvider):
        PROVIDER_TYPE = "nocaps"
        DEFAULT_MODEL = "x"

        def map_usage(self, response: Any) -> TokenUsage:
            return TokenUsage()

    with pytest.raises(NotImplementedError, match="CAPABILITIES"):
        _NoCaps().get_capabilities()


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_registry_seeds_builtin_providers_on_first_access() -> None:
    assert isinstance(ProviderRegistry.get("anthropic"), AnthropicProvider)
    assert isinstance(ProviderRegistry.get("openai"), OpenAIProvider)
    assert isinstance(ProviderRegistry.get("gemini"), GeminiProvider)
    assert isinstance(ProviderRegistry.get("bedrock"), BedrockProvider)
    assert {"anthropic", "openai", "gemini", "bedrock"} <= set(
        ProviderRegistry.types()
    )
    # OpenAICompatProvider is instance-configured (base_url + model
    # required), so it has no zero-config seed.
    assert ProviderRegistry.get("openai_compat") is None


def test_registry_resolves_anthropic_bedrock_to_bedrock_capabilities() -> None:
    # Claude-on-Bedrock via the AnthropicBedrock client is tagged
    # "anthropic_bedrock"; capability lookups must resolve it (not fall
    # back to None) with the Anthropic-family cache style and a
    # Bedrock-namespaced cheap summariser model.
    provider = ProviderRegistry.get("anthropic_bedrock")
    assert isinstance(provider, BedrockProvider)
    caps = provider.get_capabilities(
        "anthropic.claude-3-5-sonnet-20241022-v2:0"
    )
    assert caps.prompt_cache_style == "explicit_marker"
    assert caps.cheap_model_for_summariser == (
        "anthropic.claude-3-5-haiku-20241022-v1:0"
    )


def test_registry_get_unknown_type_returns_none() -> None:
    assert ProviderRegistry.get("unknown") is None


def test_register_custom_provider_under_its_provider_type() -> None:
    stub = _StubProvider()
    ProviderRegistry.register(stub)
    assert ProviderRegistry.get("stub") is stub


def test_register_with_explicit_name_overrides_the_type_key() -> None:
    stub = _StubProvider()
    ProviderRegistry.register(stub, name="my-stub")
    assert ProviderRegistry.get("my-stub") is stub
    assert ProviderRegistry.get("stub") is None


def test_reregistering_the_same_instance_is_a_noop() -> None:
    stub = _StubProvider()
    ProviderRegistry.register(stub)
    ProviderRegistry.register(stub)
    assert ProviderRegistry.get("stub") is stub


def test_register_replaces_existing_name_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = _StubProvider(), _StubProvider()
    ProviderRegistry.register(first)
    with caplog.at_level(
        logging.WARNING, logger="inkfoot.providers.registry"
    ):
        ProviderRegistry.register(second)
    assert ProviderRegistry.get("stub") is second
    assert any("replacing" in rec.message for rec in caplog.records)


def test_user_instance_registered_before_first_access_survives_seeding() -> None:
    custom = AnthropicProvider()
    ProviderRegistry.register(custom)
    # First lookup triggers seeding — the seed must not clobber the
    # explicitly registered instance.
    assert ProviderRegistry.get("anthropic") is custom


def test_register_rejects_provider_without_type_or_name() -> None:
    class _Nameless(LLMProvider):
        DEFAULT_MODEL = "x"
        CAPABILITIES = _stub_capabilities()

        def map_usage(self, response: Any) -> TokenUsage:
            return TokenUsage()

    with pytest.raises(ValueError, match="PROVIDER_TYPE"):
        ProviderRegistry.register(_Nameless())


def test_clear_reseeds_builtins_on_next_access() -> None:
    ProviderRegistry.register(_StubProvider())
    ProviderRegistry.clear()
    assert ProviderRegistry.get("stub") is None
    assert ProviderRegistry.get("anthropic") is not None
