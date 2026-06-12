"""The ``providers`` package — capability declarations + usage
mapping for every LLM provider Inkfoot understands.

Public surface:

* :class:`LLMProvider` / :class:`Capabilities` /
  :class:`TokenUsage` — the abstraction. Subclass
  :class:`LLMProvider` to bring your own provider.
* Concrete providers — :class:`AnthropicProvider`,
  :class:`OpenAIProvider`, :class:`GeminiProvider`,
  :class:`BedrockProvider`, :class:`OpenAICompatProvider`.
* :data:`ProviderRegistry` — process-global ``type → instance``
  lookup, seeded with the zero-config built-ins on first access.
  (:class:`OpenAICompatProvider` is instance-configured, so it has
  no seed — register a configured instance explicitly.)

Capability flags carry the per-provider variance; the
instrumentation loop never branches on a provider name.
"""

from inkfoot.providers._registry import ProviderRegistry
from inkfoot.providers.anthropic import AnthropicProvider
from inkfoot.providers.base import Capabilities, LLMProvider, TokenUsage
from inkfoot.providers.bedrock import BedrockProvider
from inkfoot.providers.gemini import GeminiProvider
from inkfoot.providers.openai import OpenAIProvider
from inkfoot.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "AnthropicProvider",
    "BedrockProvider",
    "Capabilities",
    "GeminiProvider",
    "LLMProvider",
    "OpenAICompatProvider",
    "OpenAIProvider",
    "ProviderRegistry",
    "TokenUsage",
]
