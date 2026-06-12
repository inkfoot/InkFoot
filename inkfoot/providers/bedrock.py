"""Bedrock provider — per-model-family capabilities + usage mapping
for Converse API responses.

One provider class covers every model family Bedrock serves
(Anthropic Claude, Meta Llama, Amazon Titan, Mistral, Cohere). Two
Bedrock-specific wrinkles relative to the first-party providers:

* **Capabilities vary per model family, not per provider.** An
  ``anthropic.``-prefixed model id keeps Anthropic's explicit-marker
  prompt caching and document blocks; the other families declare no
  caching and no document support. :meth:`BedrockProvider.
  get_capabilities` resolves the family from the model id — the
  instance default or the per-call ``model=`` argument — and
  tolerates cross-region inference-profile ids (``us.anthropic.…``).
  Unknown families resolve to the conservative no-cache shape rather
  than raising.
* **The usage shape is uniform** thanks to the Converse API: every
  family reports ``usage.inputTokens`` / ``outputTokens``, plus
  ``cacheReadInputTokens`` / ``cacheWriteInputTokens`` on caching
  models. The legacy per-family ``invoke_model`` bodies are out of
  scope — Inkfoot targets Converse.

Like Anthropic's native API, Converse's ``inputTokens`` excludes the
cached portion; :meth:`BedrockProvider.map_usage` adds the cache
fields back so the neutral ``input_tokens`` is the full prompt size.
Converse usage carries no reasoning counter, so
``reasoning_tokens`` is always 0 here.
"""

from __future__ import annotations

from typing import Any, Optional

from inkfoot.providers.base import (
    Capabilities,
    LLMProvider,
    TokenUsage,
    coerce_token_count,
)

__all__ = ["BedrockProvider"]


_ANTHROPIC_FAMILY_CAPS = Capabilities(
    supports_tool_use=True,
    supports_image_input=True,
    supports_document_block=True,
    supports_prompt_cache=True,
    prompt_cache_style="explicit_marker",
    cache_read_price_ratio=0.1,
    cache_write_price_ratio=1.25,
    supports_response_format_json=False,
    cheap_model_for_summariser=(
        "anthropic.claude-3-5-haiku-20241022-v1:0"
    ),
)

_NO_CACHE_FAMILY_CAPS = Capabilities(
    supports_tool_use=True,
    supports_image_input=False,
    supports_document_block=False,
    supports_prompt_cache=False,
    prompt_cache_style="none",
    cache_read_price_ratio=1.0,
    cache_write_price_ratio=1.0,
    supports_response_format_json=False,
    cheap_model_for_summariser=None,
)

# Model-family prefix → capability declaration. First match on the
# raw model id wins; cross-region inference profiles ("us.…",
# "eu.…") are retried with the geo segment stripped.
_BEDROCK_MODEL_CAPS: dict[str, Capabilities] = {
    "anthropic.": _ANTHROPIC_FAMILY_CAPS,
    "meta.llama": _NO_CACHE_FAMILY_CAPS,
    "amazon.titan": _NO_CACHE_FAMILY_CAPS,
    "mistral.": _NO_CACHE_FAMILY_CAPS,
    "cohere.": _NO_CACHE_FAMILY_CAPS,
}

_GEO_PREFIXES = ("us.", "eu.", "apac.", "global.")


def _family_caps(model: str) -> Capabilities:
    for prefix, caps in _BEDROCK_MODEL_CAPS.items():
        if model.startswith(prefix):
            return caps
    for geo in _GEO_PREFIXES:
        if model.startswith(geo):
            stripped = model[len(geo):]
            for prefix, caps in _BEDROCK_MODEL_CAPS.items():
                if stripped.startswith(prefix):
                    return caps
            break
    # Unknown family (new launch, custom import, full ARN): the
    # conservative shape keeps policies from acting on capabilities
    # the model may not have.
    return _NO_CACHE_FAMILY_CAPS


# (camelCase, snake_case) spellings for each usage counter. boto3
# returns camelCase dicts off the wire; snake_case is tolerated for
# hand-written fixtures.
_USAGE_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadInputTokens", "cache_read_input_tokens"),
    ("cacheWriteInputTokens", "cache_write_input_tokens"),
)


def extract_usage(response: Any) -> dict[str, Any]:
    """Pull the ``usage`` block off a Converse response into a
    camelCase dict (boto3's native spelling)."""
    if response is None:
        return {}
    if isinstance(response, dict):
        usage = response.get("usage")
    else:
        usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    if isinstance(usage, dict):
        for camel, snake in _USAGE_FIELDS:
            value = usage.get(camel, usage.get(snake))
            if value is not None:
                out[camel] = value
        return out
    for camel, snake in _USAGE_FIELDS:
        value = getattr(usage, camel, None)
        if value is None:
            value = getattr(usage, snake, None)
        if value is not None:
            out[camel] = value
    return out


def cache_status_from_usage(usage: dict[str, Any]) -> str:
    """Same quadrant logic as the native Anthropic API — Converse
    reports both a read and a write counter per call."""
    read = coerce_token_count(usage.get("cacheReadInputTokens"))
    write = coerce_token_count(usage.get("cacheWriteInputTokens"))
    if read > 0 and write > 0:
        return "partial"
    if read > 0:
        return "hit"
    if write > 0:
        return "miss"
    return "n/a"


class BedrockProvider(LLMProvider):
    """Capability flags + usage mapping for the Bedrock Converse API
    (``boto3``). Capabilities resolve per model family — see the
    module docstring."""

    PROVIDER_TYPE = "bedrock"
    DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    # CAPABILITIES stays None on purpose — they vary per model
    # family; get_capabilities() resolves them.

    def __init__(self, model: Optional[str] = None) -> None:
        self._model = model or self.DEFAULT_MODEL

    def get_capabilities(self, model: Optional[str] = None) -> Capabilities:
        return _family_caps(model or self._model)

    def map_usage(self, response: Any) -> TokenUsage:
        usage = extract_usage(response)
        cache_read = coerce_token_count(usage.get("cacheReadInputTokens"))
        cache_write = coerce_token_count(
            usage.get("cacheWriteInputTokens")
        )
        # Converse's inputTokens excludes the cached portion (the
        # Anthropic convention); the neutral overlay's input_tokens
        # is the full prompt size.
        fresh_input = coerce_token_count(usage.get("inputTokens"))
        return TokenUsage(
            input_tokens=fresh_input + cache_read + cache_write,
            output_tokens=coerce_token_count(usage.get("outputTokens")),
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
            reasoning_tokens=0,
            cache_status=cache_status_from_usage(usage),
        )
