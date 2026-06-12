"""Provider registry — process-global ``provider type → instance``
lookup.

Unlike the adapter registry (one *active* adapter at a time), every
registered provider is live simultaneously: this is a pure lookup
table for code that needs a provider's capability declaration or
usage mapping given the ``provider`` string on a call context or
event.

Zero-config built-ins are seeded lazily on first access. A
user-constructed instance (custom credentials, capability
overrides) may register over a seed: an explicit :meth:`register`
for an existing name replaces the previous instance with a WARNING
— provider instances are value-like declarations, so the newest,
most specific one wins. (Contrast
:mod:`inkfoot.adapters._registry`, which refuses duplicate names
because the active adapter gates policy registration.)

Thread-safe via a private lock.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.providers.base import LLMProvider

_LOG = logging.getLogger("inkfoot.providers.registry")


class _ProviderRegistry:
    def __init__(self) -> None:
        self._by_type: dict[str, "LLMProvider"] = {}
        self._seeded = False
        self._lock = threading.Lock()

    def _ensure_seeded_locked(self) -> None:
        """Register the zero-config built-ins. ``setdefault`` so an
        instance the user registered *before* first lookup isn't
        clobbered by its seed. Caller holds the lock."""
        if self._seeded:
            return
        # Function-level imports — the concrete modules import
        # providers.base, and seeding at module import time would
        # make that circular.
        from inkfoot.providers.anthropic import AnthropicProvider  # noqa: PLC0415
        from inkfoot.providers.bedrock import BedrockProvider  # noqa: PLC0415
        from inkfoot.providers.gemini import GeminiProvider  # noqa: PLC0415
        from inkfoot.providers.openai import OpenAIProvider  # noqa: PLC0415

        for cls in (
            AnthropicProvider,
            OpenAIProvider,
            GeminiProvider,
            BedrockProvider,
        ):
            self._by_type.setdefault(cls.PROVIDER_TYPE, cls())
        self._seeded = True

    def register(
        self, provider: "LLMProvider", *, name: Optional[str] = None
    ) -> None:
        """Add ``provider`` under ``name`` (default: its
        ``PROVIDER_TYPE``). Re-registering the same instance is a
        no-op; a different instance under an existing name replaces
        it and logs a WARNING."""
        key = name or getattr(provider, "PROVIDER_TYPE", None)
        if not key or not isinstance(key, str):
            raise ValueError(
                "provider registry: provider declares no PROVIDER_TYPE "
                "and no name= was given"
            )
        with self._lock:
            existing = self._by_type.get(key)
            if existing is provider:
                return
            self._by_type[key] = provider
        # WARNING outside the lock so a logging handler that grabs
        # another lock can't deadlock with us.
        if existing is not None:
            _LOG.warning(
                "provider registry: replacing %r (%s) with %s",
                key,
                type(existing).__name__,
                type(provider).__name__,
            )

    def get(self, provider_type: str) -> Optional["LLMProvider"]:
        """Look up a provider by type string. ``None`` if unknown."""
        with self._lock:
            self._ensure_seeded_locked()
            return self._by_type.get(provider_type)

    def types(self) -> list[str]:
        """Registered provider type strings, in insertion order."""
        with self._lock:
            self._ensure_seeded_locked()
            return list(self._by_type)

    def clear(self) -> None:
        """Drop every registration. Test-only — built-ins re-seed on
        the next access."""
        with self._lock:
            self._by_type.clear()
            self._seeded = False


# Process-global registry. One Inkfoot installation per process so a
# module-level singleton is appropriate (see policy/registry.py's
# rationale).
ProviderRegistry = _ProviderRegistry()
