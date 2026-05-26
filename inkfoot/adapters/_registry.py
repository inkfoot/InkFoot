"""Adapter registry — the process-global pointer to the active
Pattern-C adapter.

Only one adapter can be active at a time (per ADR-1-1 + the capability
matrix: the policy registration check needs a single source of truth
for ``supported_policies()``). The registry stores adapters by their
:attr:`~inkfoot.adapters.base.FrameworkAdapter.name` and rejects
duplicates so a downstream library can't silently shadow a built-in.

Thread-safe via a private lock — adapter installation may race with
a worker thread's ``register_policies`` call when a downstream init
ordering surprises us.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.adapters.base import FrameworkAdapter


class DuplicateAdapterName(ValueError):
    """Raised when two adapters declare the same :attr:`name`."""


class _AdapterRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, "FrameworkAdapter"] = {}
        self._active: Optional["FrameworkAdapter"] = None
        self._lock = threading.Lock()

    def register(self, adapter: "FrameworkAdapter") -> None:
        """Add ``adapter`` to the registry. Raises
        :class:`DuplicateAdapterName` when an adapter with the same
        ``name`` is already registered (different instance).
        Re-registering the *same instance* is a no-op.
        """
        with self._lock:
            existing = self._by_name.get(adapter.name)
            if existing is adapter:
                return
            if existing is not None:
                raise DuplicateAdapterName(
                    f"adapter registry: name {adapter.name!r} is already "
                    f"registered to {type(existing).__name__}; refusing to "
                    f"shadow with {type(adapter).__name__}. Call "
                    f"unregister({adapter.name!r}) first if this is intentional."
                )
            self._by_name[adapter.name] = adapter

    def unregister(self, name: str) -> None:
        """Remove the adapter with ``name``. Idempotent. Clears the
        active pointer if it pointed at the removed adapter."""
        with self._lock:
            removed = self._by_name.pop(name, None)
            if removed is not None and self._active is removed:
                self._active = None

    def set_active(self, adapter: "FrameworkAdapter") -> None:
        """Mark ``adapter`` as the active Pattern-C adapter. Registers
        it first if not yet known so the common
        ``register-and-activate`` path is one call."""
        with self._lock:
            existing = self._by_name.get(adapter.name)
            if existing is None:
                self._by_name[adapter.name] = adapter
            elif existing is not adapter:
                raise DuplicateAdapterName(
                    f"adapter registry: cannot activate {type(adapter).__name__}; "
                    f"name {adapter.name!r} is already registered to a "
                    f"different instance ({type(existing).__name__})."
                )
            self._active = adapter

    def clear_active(self) -> None:
        """Drop the active-adapter pointer. Tests + adapter shutdown
        use this. Does **not** remove the adapter from the registry —
        :meth:`unregister` does that."""
        with self._lock:
            self._active = None

    def get_active(self) -> Optional["FrameworkAdapter"]:
        """Return the currently-active adapter, or ``None``."""
        with self._lock:
            return self._active

    def get(self, name: str) -> Optional["FrameworkAdapter"]:
        """Look up an adapter by name. ``None`` if unknown."""
        with self._lock:
            return self._by_name.get(name)

    def names(self) -> list[str]:
        """Registered adapter names, in insertion order."""
        with self._lock:
            return list(self._by_name.keys())

    def clear(self) -> None:
        """Drop every adapter + the active pointer. Test-only — the
        production process never has a reason to call this."""
        with self._lock:
            self._by_name.clear()
            self._active = None


# Process-global registry. One Inkfoot installation per process so a
# module-level singleton is appropriate (see policy/registry.py's
# rationale).
AdapterRegistry = _AdapterRegistry()


def get_active_adapter() -> Optional["FrameworkAdapter"]:
    """Public accessor — what
    :func:`inkfoot.policy.register_policies` consults to decide
    whether a policy's supported set includes the current pattern.
    """
    return AdapterRegistry.get_active()
