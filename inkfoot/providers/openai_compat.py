"""OpenAI-compatible provider — one class for the long tail of
endpoints that speak the OpenAI Chat Completions wire protocol
(vLLM, Together, Fireworks, Anyscale, DeepInfra, Groq, LM Studio,
Ollama, …).

Unlike the first-party providers, nothing here is knowable up
front: the backends are heterogeneous, so the class is
instance-configured rather than declared at class level.

* **Conservative default capabilities.** Tool use is near-universal
  across compat servers; prompt caching, image input, and document
  blocks are not. The default declares only what every backend
  supports, so policies never act on a capability the endpoint
  lacks. Operators who know their backend can widen the declaration
  with the ``capabilities=`` override — either a full
  :class:`Capabilities` instance or a dict of field overrides
  applied on top of the conservative base.
* **The usage shape is OpenAI's.** Responses come off an OpenAI SDK
  client pointed at ``base_url``, so :meth:`OpenAICompatProvider.
  map_usage` delegates response-side mapping wholesale to the
  OpenAI provider's logic (``prompt_tokens`` is fresh + cached;
  ``cached_tokens`` / ``reasoning_tokens`` in the details blocks,
  when the backend reports them at all).
* **Pricing defaults to $0.** Self-hosted means free at the
  provider boundary — the pricing table carries an
  ``("openai_compat", "*")`` wildcard row of zeros, and operators
  on a paid compat endpoint add exact per-model rows that win over
  the wildcard.
* **Not registry-seeded.** There is no universal endpoint or model,
  so the registry can't construct a zero-config instance; operators
  register their configured instance explicitly via
  ``ProviderRegistry.register(...)``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional, Union

from inkfoot.providers.base import Capabilities, LLMProvider, TokenUsage
from inkfoot.providers.openai import OpenAIProvider

__all__ = ["OpenAICompatProvider"]


_CONSERVATIVE_COMPAT_CAPS = Capabilities(
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

# Response-side mapping is pure, so one shared mapper instance
# serves every OpenAICompatProvider.
_OPENAI_USAGE_MAPPER = OpenAIProvider()


def _resolve_capabilities(
    capabilities: Optional[Union[Capabilities, Mapping[str, Any]]],
) -> Capabilities:
    if capabilities is None:
        return _CONSERVATIVE_COMPAT_CAPS
    if isinstance(capabilities, Capabilities):
        return capabilities
    if isinstance(capabilities, Mapping):
        # Field overrides on top of the conservative base. replace()
        # re-runs Capabilities' validation, so an incoherent override
        # (e.g. a cache style without cache support) fails loudly at
        # construction rather than misleading policies later.
        return dataclasses.replace(
            _CONSERVATIVE_COMPAT_CAPS, **dict(capabilities)
        )
    raise TypeError(
        "capabilities must be a Capabilities instance or a mapping "
        f"of field overrides, not {type(capabilities).__name__}"
    )


class OpenAICompatProvider(LLMProvider):
    """Instance-configured provider for OpenAI-compatible endpoints.

    >>> provider = OpenAICompatProvider(
    ...     base_url="http://localhost:11434/v1",
    ...     model="llama3.2",
    ... )

    ``api_key`` is optional — local backends like Ollama ignore it.
    """

    PROVIDER_TYPE = "openai_compat"
    # The canonical local default — instances always pin their own
    # model; this only satisfies the abstraction's declaration.
    DEFAULT_MODEL = "llama3.2"
    # CAPABILITIES stays None on purpose — they vary per instance;
    # get_capabilities() returns the configured declaration.

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        capabilities: Optional[
            Union[Capabilities, Mapping[str, Any]]
        ] = None,
    ) -> None:
        if not base_url or not isinstance(base_url, str):
            raise ValueError(
                "OpenAICompatProvider requires a non-empty base_url"
            )
        if not model or not isinstance(model, str):
            raise ValueError(
                "OpenAICompatProvider requires a non-empty model"
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._capabilities = _resolve_capabilities(capabilities)

    def get_capabilities(self, model: Optional[str] = None) -> Capabilities:
        # Capability variance is per endpoint, not per model — the
        # operator declared what their backend supports, so the
        # model argument is accepted (abstraction contract) but
        # doesn't change the answer.
        return self._capabilities

    def map_usage(self, response: Any) -> TokenUsage:
        return _OPENAI_USAGE_MAPPER.map_usage(response)
