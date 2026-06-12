"""Gemini provider — capability declaration + usage mapping for
``GenerativeModel.generate_content`` responses, plus the
``CachedContent`` resource manager behind the ``cache_resource``
prompt-cache style.

Usage shape: Gemini reports counters on ``usage_metadata``
(``usageMetadata`` in REST/camelCase fixtures):

* ``prompt_token_count`` — full prompt size, **inclusive** of any
  cached portion (same convention as OpenAI's ``prompt_tokens``).
* ``candidates_token_count`` — generated output, excluding thinking.
* ``thoughts_token_count`` — thinking tokens on reasoning models.
  Billed at the output rate, so :meth:`GeminiProvider.map_usage`
  folds them into ``output_tokens`` and also surfaces them as the
  ``reasoning_tokens`` overlay (mirroring OpenAI's inclusive
  ``completion_tokens``).
* ``cached_content_token_count`` — the portion of the prompt served
  from a ``CachedContent`` resource.

Cache attribution: Gemini has no per-call cache *write* — the write
happens when the ``CachedContent`` resource is created. ``map_usage``
therefore always maps the cached count to ``cache_read_tokens``; the
Gemini translator re-attributes that count to
``cache_creation_tokens`` for the one call that triggered resource
creation (signalled via ``InMemoryRunState.
pending_cache_resource_creation``, set by the cache-resource arm of
``CacheControlPlacer``). The per-hour storage fee on a live resource
is a resource-level cost, not a per-call one, so it never appears in
the per-call ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Optional

from inkfoot.providers.base import (
    Capabilities,
    LLMProvider,
    TokenUsage,
    coerce_token_count,
)

__all__ = ["GEMINI_CACHE_MANAGER", "GeminiCacheManager", "GeminiProvider"]

_LOG = logging.getLogger("inkfoot.providers.gemini")

# (snake_case, camelCase) spellings for each usage counter. The SDK
# surfaces snake_case attributes; raw REST payloads and some fixtures
# carry camelCase keys.
_USAGE_FIELDS = (
    ("prompt_token_count", "promptTokenCount"),
    ("candidates_token_count", "candidatesTokenCount"),
    ("cached_content_token_count", "cachedContentTokenCount"),
    ("thoughts_token_count", "thoughtsTokenCount"),
)


def extract_usage(response: Any) -> dict[str, Any]:
    """Pull the usage block off a Gemini response into a snake_case
    dict. Accepts attribute-access (real SDK), dict-access (test
    fixtures), and camelCase REST shapes."""
    if response is None:
        return {}
    if isinstance(response, dict):
        usage = response.get("usage_metadata")
        if usage is None:
            usage = response.get("usageMetadata")
    else:
        usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    if isinstance(usage, dict):
        for snake, camel in _USAGE_FIELDS:
            value = usage.get(snake, usage.get(camel))
            if value is not None:
                out[snake] = value
        return out
    # SDK object — read fields via getattr with defaults.
    for snake, _camel in _USAGE_FIELDS:
        out[snake] = getattr(usage, snake, None)
    return out


def response_candidates(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("candidates")
    return getattr(response, "candidates", None)


def cache_status_from_usage(usage: dict[str, Any]) -> str:
    """``hit`` when any of the prompt was served from a cached
    resource, ``n/a`` otherwise. ``miss`` is decided by the
    translator on the resource-creation call — the response alone
    can't distinguish "no cache" from "cache just written"."""
    cached = coerce_token_count(usage.get("cached_content_token_count"))
    return "hit" if cached > 0 else "n/a"


class GeminiProvider(LLMProvider):
    """Capability flags + usage mapping for the Gemini API
    (``google-generativeai``). The request-patching shim lives in
    :mod:`inkfoot.shims.gemini`; the request-side attribution recipe
    in :mod:`inkfoot.normalise.gemini`."""

    PROVIDER_TYPE = "gemini"
    DEFAULT_MODEL = "gemini-1.5-pro"
    CAPABILITIES = Capabilities(
        supports_tool_use=True,
        supports_image_input=True,
        supports_document_block=True,
        supports_prompt_cache=True,
        prompt_cache_style="cache_resource",
        cache_read_price_ratio=0.25,
        cache_write_price_ratio=1.0,
        supports_response_format_json=True,
        cheap_model_for_summariser="gemini-1.5-flash",
    )

    def map_usage(self, response: Any) -> TokenUsage:
        usage = extract_usage(response)
        cached = coerce_token_count(
            usage.get("cached_content_token_count")
        )
        thoughts = coerce_token_count(usage.get("thoughts_token_count"))
        return TokenUsage(
            # Gemini's prompt_token_count includes the cached portion.
            input_tokens=coerce_token_count(
                usage.get("prompt_token_count")
            ),
            # candidates excludes thinking; Gemini bills thinking at
            # the output rate, so fold it in (OpenAI-style inclusive
            # output) and surface it separately as reasoning.
            output_tokens=coerce_token_count(
                usage.get("candidates_token_count")
            )
            + thoughts,
            cache_read_tokens=cached,
            cache_creation_tokens=0,  # re-attributed by the translator
            reasoning_tokens=thoughts,
            cache_status=cache_status_from_usage(usage),
        )


class GeminiCacheManager:
    """Create-or-reuse ``CachedContent`` resources, keyed on a
    fingerprint of ``(model, system_instruction, tools)``.

    The cache-resource arm of ``CacheControlPlacer`` calls
    :meth:`get_or_create` per LLM call; the first call per
    fingerprint creates the provider-side resource, every later call
    gets the memoised handle back. Creation failures are memoised
    too, so an oversized-but-rejected prompt doesn't retry a doomed
    network call per turn.

    Never raises — a ``(None, False)`` return means "no resource";
    callers degrade to advice-only behaviour.
    """

    def __init__(self) -> None:
        self._resources: dict[str, Any] = {}
        self._failed: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(
        model: str,
        system_instruction: Any = None,
        tools: Any = None,
    ) -> str:
        """Stable digest of the cacheable prefix. ``default=str``
        keeps exotic tool objects from raising; their reprs are
        stable per object, so dict-shaped inputs (the common case)
        deduplicate across calls and instances."""
        canonical = json.dumps(
            {
                "model": model,
                "system_instruction": system_instruction,
                "tools": tools,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_or_create(
        self,
        *,
        model: str,
        system_instruction: Any = None,
        tools: Any = None,
        ttl: Any = None,
    ) -> tuple[Optional[Any], bool]:
        """Return ``(resource, created)``. ``(None, False)`` when the
        SDK isn't importable or creation failed (memoised per
        fingerprint)."""
        key = self.fingerprint(model, system_instruction, tools)
        with self._lock:
            cached = self._resources.get(key)
            if cached is not None:
                return cached, False
            if key in self._failed:
                return None, False
        # Creation happens outside the lock — it's a network call.
        # Two racing callers may both create; the loser's resource is
        # simply never referenced and expires with its TTL.
        try:
            from google.generativeai import caching  # noqa: PLC0415

            create_kwargs: dict[str, Any] = {}
            if system_instruction is not None:
                create_kwargs["system_instruction"] = system_instruction
            if tools is not None:
                create_kwargs["tools"] = tools
            if ttl is not None:
                create_kwargs["ttl"] = ttl
            resource = caching.CachedContent.create(
                model=model, **create_kwargs
            )
        except Exception:  # pylint: disable=broad-except
            _LOG.warning(
                "gemini cache: CachedContent creation failed for model "
                "%s; falling back to advice-only",
                model,
                exc_info=True,
            )
            with self._lock:
                self._failed.add(key)
            return None, False
        with self._lock:
            existing = self._resources.get(key)
            if existing is not None:
                return existing, False
            self._resources[key] = resource
        return resource, True

    def reset(self) -> None:
        """Drop every memoised resource + failure. Test-only — the
        provider-side resources are left to expire via TTL."""
        with self._lock:
            self._resources.clear()
            self._failed.clear()


# Process-global manager. One Inkfoot installation per process, and
# CachedContent handles are value-like (a name string + model), so a
# module-level singleton mirrors the provider registry's rationale.
GEMINI_CACHE_MANAGER = GeminiCacheManager()
