"""Anthropic Agent SDK adapter — Pattern-C wrap for the Anthropic
Agent SDK's ``Agent.run`` + tool-dispatch layer.

Anthropic Agent adapter; mirrors the
OpenAI Agents SDK adapter shape:

* Wrap ``Agent.run`` / ``Agent.run_async`` so the loop is scoped
  under an :func:`inkfoot.agent_run`.
* Emit ``tool_dispatched`` events on each tool invocation.

The Anthropic Agent SDK is newer than the OpenAI one (the SDK GA'd
mid-2026); the adapter pins against the latest stable's known
surface and degrades gracefully if internal method names drift.

Wrapping primitives come from :mod:`inkfoot.adapters._shared`
(review finding #3 — neither sibling adapter "owns" the
helpers).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters._shared import (
    TOOL_DISPATCH_CANDIDATES,
    install_attr,
    wrap_run_method,
    wrap_tool_dispatcher,
)

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.anthropic_agent")

_INSTRUMENTED_MARKER = "_inkfoot_anthropic_agent_instrumentation"


class _AnthropicAgentInstrumentation:
    """Teardown handle returned by :meth:`AnthropicAgentAdapter.instrument`.

    Symmetric with :class:`~inkfoot.adapters.openai_agents._OpenAIAgentsInstrumentation`
    — ``shutdown()`` unwraps the patches *and* releases the adapter's
    install count so the active-pointer auto-clears when the last
    live instrumentation goes away (review finding #4).
    """

    def __init__(
        self,
        adapter: "AnthropicAgentAdapter",
        target: Any,
        restorers: list[Callable[[], None]],
    ) -> None:
        self._adapter = adapter
        self._target = target
        self._restorers = restorers
        self._shutdown = False

    def shutdown(self) -> None:
        if self._shutdown:
            return
        for restorer in reversed(self._restorers):
            try:
                restorer()
            except Exception:  # pragma: no cover
                _LOG.warning("restore step raised", exc_info=True)
        try:
            delattr(self._target, _INSTRUMENTED_MARKER)
        except AttributeError:  # pragma: no cover
            pass
        self._shutdown = True
        self._adapter._release_install()


class AnthropicAgentAdapter:
    """Pattern-C adapter for Anthropic's Agent SDK."""

    name = "anthropic_agent"

    def __init__(self) -> None:
        self._install_count = 0

    def detect(self) -> bool:
        try:
            import anthropic_agent  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _AnthropicAgentInstrumentation:
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _AnthropicAgentInstrumentation):
            return existing

        restorers: list[Callable[[], None]] = []

        for method_name in ("run", "run_async"):
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_run_method(original, task=task or "anthropic_agent")
            install_attr(target, method_name, wrapped, restorers)

        for method_name in TOOL_DISPATCH_CANDIDATES:
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_tool_dispatcher(original)
            install_attr(target, method_name, wrapped, restorers)

        instrumentation = _AnthropicAgentInstrumentation(
            self, target, restorers
        )
        try:
            setattr(target, _INSTRUMENTED_MARKER, instrumentation)
        except (AttributeError, TypeError):
            pass

        try:
            AdapterRegistry.set_active(self)
        except Exception:  # pragma: no cover
            _LOG.warning("activate failed", exc_info=True)
        self._install_count += 1

        return instrumentation

    def _release_install(self) -> None:
        """Decrement the install count and auto-clear the active
        pointer when zero (review finding #4)."""
        if self._install_count > 0:
            self._install_count -= 1
        if self._install_count == 0:
            active = AdapterRegistry.get_active()
            if active is self:
                AdapterRegistry.clear_active()

    def supported_policies(self) -> set[type["Policy"]]:
        """Same observation-only posture as the OpenAI Agents adapter — empty
        set lets the current observation policies through the
        pattern-fallback path; future versions can enumerate modification
        policies here."""
        return set()

    def shutdown(self) -> None:
        """Force-deactivate; usually unnecessary because the per-
        instrumentation handle's ``shutdown()`` auto-deactivates."""
        AdapterRegistry.clear_active()
        self._install_count = 0


_default_adapter = AnthropicAgentAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _AnthropicAgentInstrumentation:
    """User-facing convenience — equivalent to
    ``AnthropicAgentAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)
