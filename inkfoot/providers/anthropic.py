"""Anthropic provider — capability declaration + usage mapping for
``messages.create`` responses.

The response-side helpers here (usage extraction, cache status,
thinking-token count) are shared with the Anthropic translator in
:mod:`inkfoot.normalise.anthropic`: the translator re-tokenises the
*request* side itself, but everything read off the response comes
through :meth:`AnthropicProvider.map_usage` so the two can't drift.
"""

from __future__ import annotations

from typing import Any

from inkfoot.providers.base import (
    Capabilities,
    LLMProvider,
    TokenUsage,
    coerce_token_count,
)

__all__ = ["AnthropicProvider"]


def extract_usage(response: Any) -> dict[str, Any]:
    """Accept both attribute-access (real SDK) and dict-access (test
    fixtures) shapes."""
    if response is None:
        return {}
    if isinstance(response, dict):
        usage = response.get("usage", {})
    else:
        usage = getattr(response, "usage", {})
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    # SDK object — read fields via getattr with defaults.
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(
            usage, "cache_read_input_tokens", 0
        ),
        "cache_creation_input_tokens": getattr(
            usage, "cache_creation_input_tokens", 0
        ),
        "thinking_tokens": getattr(usage, "thinking_tokens", None),
    }


def response_content(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("content")
    return getattr(response, "content", None)


def cache_status_from_usage(usage: dict[str, Any]) -> str:
    """Coarse cache classification from usage. ``hit`` when any
    cache_read, ``partial`` when both read + write, ``miss`` when
    only write, ``n/a`` when neither."""
    read = coerce_token_count(usage.get("cache_read_input_tokens"))
    write = coerce_token_count(usage.get("cache_creation_input_tokens"))
    if read > 0 and write > 0:
        return "partial"
    if read > 0:
        return "hit"
    if write > 0:
        return "miss"
    return "n/a"


def reasoning_token_count(response: Any) -> int:
    """Sum of token counts on ``thinking`` content blocks from the
    assistant response — Anthropic's extended-thinking models surface
    these alongside text blocks. Zero on models without thinking."""
    if response is None:
        return 0
    usage = extract_usage(response)
    # Some Anthropic SDKs surface a top-level usage.thinking_tokens.
    thinking = usage.get("thinking_tokens")
    if isinstance(thinking, int) and not isinstance(thinking, bool) and thinking >= 0:
        return thinking
    # Otherwise sum token counts across ``thinking`` content blocks.
    content = response_content(response)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            tokens = block.get("tokens")
            if isinstance(tokens, int) and tokens >= 0:
                total += tokens
    return total


class AnthropicProvider(LLMProvider):
    """Capability flags + usage mapping for the Anthropic Messages
    API. The request-patching shim lives in
    :mod:`inkfoot.shims.anthropic`; the request-side attribution
    recipe in :mod:`inkfoot.normalise.anthropic`."""

    PROVIDER_TYPE = "anthropic"
    DEFAULT_MODEL = "claude-sonnet-4-6"
    CAPABILITIES = Capabilities(
        supports_tool_use=True,
        supports_image_input=True,
        supports_document_block=True,
        supports_prompt_cache=True,
        prompt_cache_style="explicit_marker",
        cache_read_price_ratio=0.1,
        cache_write_price_ratio=1.25,
        supports_response_format_json=False,
        cheap_model_for_summariser="claude-haiku-4-5",
    )

    def map_usage(self, response: Any) -> TokenUsage:
        usage = extract_usage(response)
        cache_read = coerce_token_count(
            usage.get("cache_read_input_tokens")
        )
        cache_creation = coerce_token_count(
            usage.get("cache_creation_input_tokens")
        )
        # Anthropic's input_tokens excludes the cached portion; the
        # neutral overlay's input_tokens is the full prompt size.
        fresh_input = coerce_token_count(usage.get("input_tokens"))
        return TokenUsage(
            input_tokens=fresh_input + cache_read + cache_creation,
            output_tokens=coerce_token_count(usage.get("output_tokens")),
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            reasoning_tokens=reasoning_token_count(response),
            cache_status=cache_status_from_usage(usage),
        )
