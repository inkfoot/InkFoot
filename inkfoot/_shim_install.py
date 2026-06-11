"""SDK detection + shim install/uninstall mechanics.

``instrument()`` calls into :func:`install_shims` once per process.
The function probes which SDKs are importable and installs only
those — never crashes on a missing SDK, honouring the "detect
rather than require" contract.

Shims are tracked here so :func:`uninstall_shims` can cleanly
reverse the patch (used by tests + the atexit shutdown).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.shims.anthropic import AnthropicShim
    from inkfoot.shims.openai import OpenAIShim
    from inkfoot.storage import Storage


_LOG = logging.getLogger("inkfoot.shim_install")


def _try_import(name: str) -> bool:
    """Return True iff ``import <name>`` succeeds."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# Module-level tracking of currently-installed shims; populated by
# :func:`install_shims` and drained by :func:`uninstall_shims`.
_installed: list[Any] = []


def install_shims(
    *,
    storage: "Storage",
    capture_mode_getter: Callable[[], str],
    sdks: Optional[list[str]] = None,
) -> list[str]:
    """Install whichever SDK shims are available. Returns the list
    of provider names that were actually installed.

    ``sdks`` is an explicit allow-list — when ``None``, every
    supported SDK is auto-detected. Passing ``["anthropic"]``
    restricts to just Anthropic even if OpenAI is also importable
    (useful for tests).
    """
    from inkfoot.shims.anthropic import AnthropicShim  # noqa: PLC0415
    from inkfoot.shims.openai import OpenAIShim  # noqa: PLC0415

    allow = set(sdks) if sdks is not None else None
    installed: list[str] = []

    def _want(name: str) -> bool:
        if allow is None:
            return _try_import(name)
        return name in allow and _try_import(name)

    if _want("anthropic"):
        shim = AnthropicShim(storage, capture_mode_getter)
        if shim.install():
            _installed.append(shim)
            installed.append("anthropic")
    if _want("openai"):
        shim = OpenAIShim(storage, capture_mode_getter)
        if shim.install():
            _installed.append(shim)
            installed.append("openai")

    return installed


def uninstall_shims() -> None:
    """Restore the original SDK callables. Idempotent."""
    while _installed:
        shim = _installed.pop()
        try:
            shim.uninstall()
        except Exception:  # pylint: disable=broad-except
            _LOG.warning(
                "shim.uninstall() raised for %s; ignored",
                type(shim).__name__,
                exc_info=True,
            )


def installed_providers() -> list[str]:
    """Return the names of providers currently shimmed (in
    installation order). Mainly for diagnostics."""
    out: list[str] = []
    for shim in _installed:
        name = type(shim).__name__.replace("Shim", "").lower()
        out.append(name)
    return out
