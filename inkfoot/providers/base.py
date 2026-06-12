"""Provider abstraction — capability flags + usage mapping for
every LLM provider Inkfoot understands.

Two ideas keep the rest of the codebase provider-agnostic:

* :class:`Capabilities` — a frozen declaration of what one provider
  supports (tool use, prompt caching and its style, JSON response
  mode, ...). Policies and renderers branch on these flags; the
  instrumentation loop itself never branches on a provider name.
* :class:`TokenUsage` — the provider-neutral usage overlay read off
  one response. Every provider names and nests its billing counters
  differently; :meth:`LLMProvider.map_usage` folds each shape into
  this one.

Concrete providers subclass :class:`LLMProvider` and are looked up
by their ``PROVIDER_TYPE`` string via
:data:`inkfoot.providers._registry.ProviderRegistry`. Adding a
provider is additive: implement the class, register it, add a
pricing row — no changes to the instrumentation loop.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

__all__ = [
    "Capabilities",
    "LLMProvider",
    "PROMPT_CACHE_STYLES",
    "TokenUsage",
    "coerce_token_count",
]


# How a provider's prompt cache is requested:
#
# * "explicit_marker" — the request carries cache markers on the
#   blocks to cache (Anthropic ``cache_control``).
# * "automatic" — the provider caches transparently; nothing to
#   place in the request (OpenAI).
# * "cache_resource" — the caller creates an explicit cached-content
#   resource up front and references it per call (Gemini
#   ``CachedContent``).
# * "none" — no prompt cache.
PROMPT_CACHE_STYLES: frozenset[str] = frozenset(
    {"explicit_marker", "automatic", "cache_resource", "none"}
)

_VALID_CACHE_STATUSES = frozenset({"hit", "partial", "miss", "n/a"})


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What one provider supports.

    * ``supports_tool_use`` — accepts a tools/function array.
    * ``supports_image_input`` — image content blocks in messages.
    * ``supports_document_block`` — document/PDF content blocks.
    * ``supports_prompt_cache`` — any prompt-caching mechanism.
    * ``prompt_cache_style`` — one of :data:`PROMPT_CACHE_STYLES`;
      must be ``"none"`` exactly when ``supports_prompt_cache`` is
      ``False``.
    * ``cache_read_price_ratio`` — price of a cache-read token
      relative to a fresh input token.
    * ``cache_write_price_ratio`` — price of a cache-write token
      relative to a fresh input token (0.0 when writes aren't
      billed).
    * ``supports_response_format_json`` — native JSON response mode.
    * ``cheap_model_for_summariser`` — the model the summariser
      policy should call for this provider, or ``None`` when the
      provider has no obvious cheap tier (the policy then falls back
      to truncation).
    """

    supports_tool_use: bool
    supports_image_input: bool
    supports_document_block: bool
    supports_prompt_cache: bool
    prompt_cache_style: str
    cache_read_price_ratio: float
    cache_write_price_ratio: float
    supports_response_format_json: bool
    cheap_model_for_summariser: Optional[str]

    def __post_init__(self) -> None:
        if self.prompt_cache_style not in PROMPT_CACHE_STYLES:
            raise ValueError(
                f"Capabilities: invalid prompt_cache_style "
                f"{self.prompt_cache_style!r}; expected one of "
                f"{sorted(PROMPT_CACHE_STYLES)}"
            )
        if self.supports_prompt_cache and self.prompt_cache_style == "none":
            raise ValueError(
                "Capabilities: supports_prompt_cache=True requires a "
                "prompt_cache_style other than 'none'"
            )
        if not self.supports_prompt_cache and self.prompt_cache_style != "none":
            raise ValueError(
                f"Capabilities: prompt_cache_style "
                f"{self.prompt_cache_style!r} requires "
                f"supports_prompt_cache=True"
            )
        if self.cache_read_price_ratio < 0 or self.cache_write_price_ratio < 0:
            raise ValueError(
                "Capabilities: cache price ratios must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-neutral usage overlay for one LLM response.

    ``input_tokens`` is the **full prompt size** — fresh plus
    cache-read plus cache-written tokens. Providers report this
    differently (Anthropic's ``usage.input_tokens`` excludes the
    cached portion; OpenAI's ``prompt_tokens`` includes it); every
    :meth:`LLMProvider.map_usage` implementation normalises to the
    inclusive meaning, so the fresh portion is uniformly
    ``input_tokens - cache_read_tokens - cache_creation_tokens``.

    Counters are never negative — ``map_usage`` implementations
    clamp via :func:`coerce_token_count` rather than propagate a
    provider's bookkeeping glitch.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cache_status: str = "n/a"  # "hit" | "partial" | "miss" | "n/a"

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"TokenUsage: {name} must be a non-negative int, "
                    f"got {value!r}"
                )
        if self.cache_status not in _VALID_CACHE_STATUSES:
            raise ValueError(
                f"TokenUsage: invalid cache_status "
                f"{self.cache_status!r}; expected one of "
                f"{sorted(_VALID_CACHE_STATUSES)}"
            )


def coerce_token_count(value: Any) -> int:
    """Best-effort coercion of a provider-reported counter to a
    non-negative int. ``None``, bools, negatives, and un-castable
    values land at 0 — ``map_usage`` must never raise mid-shim."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


class LLMProvider(abc.ABC):
    """Base class for one provider integration.

    Concrete subclasses declare:

    * ``PROVIDER_TYPE`` — the registry key, and the ``provider``
      string stamped on events and pricing rows.
    * ``DEFAULT_MODEL`` — the model used when a caller doesn't pin
      one.
    * ``CAPABILITIES`` — the static capability declaration.
      Providers whose capabilities vary per model or per instance
      leave it ``None`` and override :meth:`get_capabilities`.

    and implement :meth:`map_usage`.
    """

    PROVIDER_TYPE: ClassVar[str]
    DEFAULT_MODEL: ClassVar[str]
    CAPABILITIES: ClassVar[Optional[Capabilities]] = None

    def get_capabilities(self, model: Optional[str] = None) -> Capabilities:
        """Capability declaration for ``model``. The base
        implementation ignores ``model`` and returns the class-level
        :attr:`CAPABILITIES` — override for per-model variance."""
        caps = self.CAPABILITIES
        if caps is None:
            raise NotImplementedError(
                f"{type(self).__name__} declares no CAPABILITIES; set "
                "the class attribute or override get_capabilities()."
            )
        return caps

    @abc.abstractmethod
    def map_usage(self, response: Any) -> TokenUsage:
        """Fold the provider's usage block into :class:`TokenUsage`.

        Tolerant by contract: ``response`` may be ``None`` (errored
        call), a dict fixture, or a live SDK object; missing or
        malformed counters land at 0. Never raises.
        """
        raise NotImplementedError  # pragma: no cover
