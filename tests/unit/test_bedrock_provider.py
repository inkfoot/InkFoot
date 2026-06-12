"""BedrockProvider tests.

Covers:
- Per-family capability resolution: ``anthropic.``-prefixed model
  ids keep Anthropic's explicit-marker caching and document blocks;
  Llama / Titan / Mistral / Cohere ids declare no caching; unknown
  families resolve to the conservative no-cache shape without
  raising; cross-region inference-profile ids (``us.anthropic.…``)
  resolve through the geo prefix.
- The per-call ``model=`` argument overrides the instance default.
- ``extract_usage`` tolerates camelCase (boto3 wire shape),
  snake_case (hand-written fixtures), attribute-style objects, and
  ``None``.
- ``map_usage``: Converse's ``inputTokens`` excludes the cached
  portion, so the neutral ``input_tokens`` adds reads + writes back;
  the read/write quadrant maps to hit / miss / partial / n-a;
  ``reasoning_tokens`` is always 0; garbage counters coerce to 0.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inkfoot.providers.bedrock import (
    BedrockProvider,
    cache_status_from_usage,
    extract_usage,
)

_ANTHROPIC_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"
_LLAMA_MODEL = "meta.llama3-2-3b-instruct-v1:0"


def _converse_response(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict:
    usage: dict = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
    }
    if cache_read:
        usage["cacheReadInputTokens"] = cache_read
    if cache_write:
        usage["cacheWriteInputTokens"] = cache_write
    return {
        "output": {
            "message": {"role": "assistant", "content": [{"text": "ack"}]}
        },
        "stopReason": "end_turn",
        "usage": usage,
    }


# ----------------------------------------------------------------------
# Per-family capability resolution
# ----------------------------------------------------------------------


def test_anthropic_family_keeps_explicit_marker_caching() -> None:
    caps = BedrockProvider(_ANTHROPIC_MODEL).get_capabilities()
    assert caps.supports_prompt_cache is True
    assert caps.prompt_cache_style == "explicit_marker"
    assert caps.supports_document_block is True
    assert caps.supports_image_input is True
    assert caps.cache_read_price_ratio == pytest.approx(0.1)
    assert caps.cache_write_price_ratio == pytest.approx(1.25)


def test_llama_family_declares_no_caching() -> None:
    caps = BedrockProvider(_LLAMA_MODEL).get_capabilities()
    assert caps.supports_prompt_cache is False
    assert caps.prompt_cache_style == "none"
    assert caps.supports_document_block is False
    assert caps.cheap_model_for_summariser is None


@pytest.mark.parametrize(
    "model",
    [
        "meta.llama3-2-3b-instruct-v1:0",
        "amazon.titan-text-express-v1",
        "mistral.mistral-large-2407-v1:0",
        "cohere.command-r-plus-v1:0",
    ],
    ids=["llama", "titan", "mistral", "cohere"],
)
def test_non_anthropic_families_share_the_no_cache_shape(
    model: str,
) -> None:
    caps = BedrockProvider(model).get_capabilities()
    assert caps.supports_tool_use is True
    assert caps.supports_prompt_cache is False
    assert caps.prompt_cache_style == "none"
    assert caps.cache_read_price_ratio == pytest.approx(1.0)
    assert caps.cache_write_price_ratio == pytest.approx(1.0)


def test_default_model_is_anthropic_family() -> None:
    caps = BedrockProvider().get_capabilities()
    assert caps.prompt_cache_style == "explicit_marker"


def test_per_call_model_argument_overrides_instance_default() -> None:
    provider = BedrockProvider(_ANTHROPIC_MODEL)
    caps = provider.get_capabilities(model=_LLAMA_MODEL)
    assert caps.supports_prompt_cache is False
    # The instance default is untouched.
    assert provider.get_capabilities().supports_prompt_cache is True


@pytest.mark.parametrize(
    "model",
    [
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "eu.anthropic.claude-3-5-haiku-20241022-v1:0",
        "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "global.anthropic.claude-3-5-sonnet-20241022-v2:0",
    ],
    ids=["us", "eu", "apac", "global"],
)
def test_cross_region_inference_profile_resolves_through_geo_prefix(
    model: str,
) -> None:
    caps = BedrockProvider(model).get_capabilities()
    assert caps.prompt_cache_style == "explicit_marker"


def test_geo_prefixed_llama_resolves_to_no_cache() -> None:
    caps = BedrockProvider(
        "us.meta.llama3-2-3b-instruct-v1:0"
    ).get_capabilities()
    assert caps.supports_prompt_cache is False


def test_unknown_family_resolves_conservatively_without_raising() -> None:
    caps = BedrockProvider("ai21.jamba-1-5-large-v1:0").get_capabilities()
    assert caps.supports_prompt_cache is False
    assert caps.prompt_cache_style == "none"
    assert caps.cheap_model_for_summariser is None


# ----------------------------------------------------------------------
# extract_usage
# ----------------------------------------------------------------------


def test_extract_usage_of_none_is_empty() -> None:
    assert extract_usage(None) == {}


def test_extract_usage_of_response_without_usage_is_empty() -> None:
    assert extract_usage({"output": {}}) == {}
    assert extract_usage(SimpleNamespace()) == {}


def test_extract_usage_reads_camel_case_wire_shape() -> None:
    usage = extract_usage(
        _converse_response(
            input_tokens=100, output_tokens=9, cache_read=30
        )
    )
    assert usage == {
        "inputTokens": 100,
        "outputTokens": 9,
        "cacheReadInputTokens": 30,
    }


def test_extract_usage_tolerates_snake_case_fixtures() -> None:
    usage = extract_usage(
        {
            "usage": {
                "input_tokens": 50,
                "output_tokens": 4,
                "cache_write_input_tokens": 20,
            }
        }
    )
    assert usage == {
        "inputTokens": 50,
        "outputTokens": 4,
        "cacheWriteInputTokens": 20,
    }


def test_extract_usage_reads_attribute_style_objects() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(inputTokens=70, outputTokens=8)
    )
    usage = extract_usage(response)
    assert usage == {"inputTokens": 70, "outputTokens": 8}


# ----------------------------------------------------------------------
# cache_status_from_usage
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("read", "write", "expected"),
    [
        (0, 0, "n/a"),
        (30, 0, "hit"),
        (0, 40, "miss"),
        (30, 40, "partial"),
    ],
)
def test_cache_status_quadrants(
    read: int, write: int, expected: str
) -> None:
    usage: dict = {}
    if read:
        usage["cacheReadInputTokens"] = read
    if write:
        usage["cacheWriteInputTokens"] = write
    assert cache_status_from_usage(usage) == expected


# ----------------------------------------------------------------------
# map_usage
# ----------------------------------------------------------------------


def test_map_usage_adds_cache_counters_back_into_input() -> None:
    usage = BedrockProvider().map_usage(
        _converse_response(
            input_tokens=100,
            output_tokens=5,
            cache_read=30,
            cache_write=20,
        )
    )
    # Converse's inputTokens excludes the cached portion; the
    # neutral input_tokens is the full prompt.
    assert usage.input_tokens == 150
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 30
    assert usage.cache_creation_tokens == 20
    assert usage.cache_status == "partial"


def test_map_usage_without_cache_counters_is_na() -> None:
    usage = BedrockProvider().map_usage(
        _converse_response(input_tokens=40, output_tokens=7)
    )
    assert usage.input_tokens == 40
    assert usage.output_tokens == 7
    assert usage.cache_status == "n/a"


def test_map_usage_read_only_is_a_hit() -> None:
    usage = BedrockProvider().map_usage(
        _converse_response(input_tokens=10, output_tokens=1, cache_read=90)
    )
    assert usage.input_tokens == 100
    assert usage.cache_status == "hit"


def test_map_usage_write_only_is_a_miss() -> None:
    usage = BedrockProvider().map_usage(
        _converse_response(
            input_tokens=10, output_tokens=1, cache_write=90
        )
    )
    assert usage.input_tokens == 100
    assert usage.cache_status == "miss"


def test_map_usage_reasoning_tokens_are_always_zero() -> None:
    usage = BedrockProvider().map_usage(
        _converse_response(input_tokens=10, output_tokens=50)
    )
    assert usage.reasoning_tokens == 0


def test_map_usage_reads_attribute_style_objects() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            inputTokens=70, outputTokens=9, cacheReadInputTokens=30
        )
    )
    usage = BedrockProvider().map_usage(response)
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 30
    assert usage.cache_status == "hit"


def test_map_usage_coerces_garbage_counters_to_zero() -> None:
    usage = BedrockProvider().map_usage(
        {
            "usage": {
                "inputTokens": None,
                "outputTokens": "garbage",
                "cacheReadInputTokens": {"weird": 1},
                "cacheWriteInputTokens": -3,
            }
        }
    )
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cache_status == "n/a"
