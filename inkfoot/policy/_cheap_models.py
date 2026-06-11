"""Per-provider cheap-model capability map.

Summariser-style policies route their helper calls to the *same
provider* as the active investigation (no credential proliferation)
but to that provider's designated cheap model. Providers without an
entry get ``None`` — callers fall back to mechanical truncation.

The map is module-level and mutable on purpose: an OpenAI-compatible
gateway deployment can point its provider key at whatever small model
the gateway serves before instrumenting.
"""

from __future__ import annotations

from typing import Optional

CHEAP_MODEL_FOR_SUMMARISER: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
}


def cheap_model_for(provider: str) -> Optional[str]:
    """The provider's designated cheap summariser model, or ``None``
    when the provider hasn't declared one."""
    return CHEAP_MODEL_FOR_SUMMARISER.get(provider)
