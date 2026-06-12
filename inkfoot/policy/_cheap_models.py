"""Per-provider cheap-model resolution for summariser policies.

Summariser-style policies route their helper calls to the *same
provider* as the active investigation (no credential proliferation)
but to that provider's designated cheap model. The source of truth
is the provider's capability declaration —
``Capabilities.cheap_model_for_summariser``, looked up through the
provider registry — so a provider declares the fact once and every
consumer agrees. Providers that declare ``None``, and provider
strings missing from the registry, resolve to ``None``; callers
fall back to mechanical truncation.

``CHEAP_MODEL_FOR_SUMMARISER`` is a module-level mutable *override*
map that wins over the capability declaration. It ships empty; an
OpenAI-compatible gateway deployment can point its provider key at
whatever small model the gateway serves before instrumenting.
"""

from __future__ import annotations

from typing import Optional

CHEAP_MODEL_FOR_SUMMARISER: dict[str, str] = {}


def cheap_model_for(
    provider: str, model: Optional[str] = None
) -> Optional[str]:
    """The provider's designated cheap summariser model, or ``None``
    when the provider hasn't declared one.

    Resolution order: the ``CHEAP_MODEL_FOR_SUMMARISER`` override
    map first, then the registry's capability declaration for
    ``model`` (capabilities can vary per model — Bedrock's do).
    """
    override = CHEAP_MODEL_FOR_SUMMARISER.get(provider)
    if override is not None:
        return override
    # Function-level import keeps the policy package import-light.
    from inkfoot.providers import ProviderRegistry  # noqa: PLC0415

    declared = ProviderRegistry.get(provider)
    if declared is None:
        return None
    return declared.get_capabilities(model).cheap_model_for_summariser
