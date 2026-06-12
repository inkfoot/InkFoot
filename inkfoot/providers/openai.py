"""OpenAI provider — capability declaration + usage mapping for
``chat.completions.create`` responses.

The response-side helpers are shared with the OpenAI translator in
:mod:`inkfoot.normalise.openai` via
:meth:`OpenAIProvider.map_usage`, mirroring the Anthropic split:
request-side tokenisation stays in the translator, response-side
counters come from here.

Key billing differences from Anthropic:

* ``usage.prompt_tokens`` aggregates *fresh + cached* input tokens
  (already the neutral overlay's inclusive meaning). Cached input
  lives in ``usage.prompt_tokens_details.cached_tokens``.
* OpenAI doesn't bill cache *writes* — ``cache_creation_tokens`` is
  always 0, and the cache status can never be ``"miss"`` or
  ``"partial"`` (there is no observable write).
* Reasoning tokens (o-series) live in
  ``usage.completion_tokens_details.reasoning_tokens``.
"""

from __future__ import annotations

from typing import Any

from inkfoot.providers.base import (
    Capabilities,
    LLMProvider,
    TokenUsage,
    coerce_token_count,
)

__all__ = ["OpenAIProvider"]


def extract_usage(response: Any) -> dict[str, Any]:
    """Accept both attribute-access (real SDK) and dict-access (test
    fixtures) shapes."""
    if response is None:
        return {}
    if isinstance(response, dict):
        usage = response.get("usage") or {}
        return usage if isinstance(usage, dict) else {}
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }
    pt_details = getattr(usage, "prompt_tokens_details", None)
    if pt_details is not None:
        out["prompt_tokens_details"] = {
            "cached_tokens": getattr(pt_details, "cached_tokens", 0),
        }
    ct_details = getattr(usage, "completion_tokens_details", None)
    if ct_details is not None:
        out["completion_tokens_details"] = {
            "reasoning_tokens": getattr(ct_details, "reasoning_tokens", 0),
        }
    return out


def _details(usage: dict[str, Any], key: str) -> dict[str, Any]:
    details = usage.get(key)
    return details if isinstance(details, dict) else {}


def cache_status_from_usage(usage: dict[str, Any]) -> str:
    cached = coerce_token_count(
        _details(usage, "prompt_tokens_details").get("cached_tokens")
    )
    if cached > 0:
        return "hit"
    return "n/a"


class OpenAIProvider(LLMProvider):
    """Capability flags + usage mapping for the OpenAI Chat
    Completions API. The request-patching shim lives in
    :mod:`inkfoot.shims.openai`; the request-side attribution recipe
    in :mod:`inkfoot.normalise.openai`."""

    PROVIDER_TYPE = "openai"
    DEFAULT_MODEL = "gpt-4o"
    CAPABILITIES = Capabilities(
        supports_tool_use=True,
        supports_image_input=True,
        supports_document_block=False,
        supports_prompt_cache=True,
        prompt_cache_style="automatic",
        cache_read_price_ratio=0.5,
        cache_write_price_ratio=0.0,
        supports_response_format_json=True,
        cheap_model_for_summariser="gpt-4o-mini",
    )

    def map_usage(self, response: Any) -> TokenUsage:
        usage = extract_usage(response)
        return TokenUsage(
            input_tokens=coerce_token_count(usage.get("prompt_tokens")),
            output_tokens=coerce_token_count(
                usage.get("completion_tokens")
            ),
            cache_read_tokens=coerce_token_count(
                _details(usage, "prompt_tokens_details").get(
                    "cached_tokens"
                )
            ),
            cache_creation_tokens=0,  # no billed cache writes
            reasoning_tokens=coerce_token_count(
                _details(usage, "completion_tokens_details").get(
                    "reasoning_tokens"
                )
            ),
            cache_status=cache_status_from_usage(usage),
        )
