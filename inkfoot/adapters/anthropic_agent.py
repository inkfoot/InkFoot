"""Anthropic Agent SDK adapter — Pattern-C wrap for the Anthropic
Agent SDK's ``Agent.run`` + tool-dispatch layer.

Per phase-1-explain §4.1.3 + E1-S4 task list, this mirrors the
OpenAI Agents SDK adapter shape:

* Wrap ``Agent.run`` / ``Agent.run_async`` so the loop is scoped
  under an :func:`inkfoot.agent_run`.
* Emit ``tool_dispatched`` events on each tool invocation.

The Anthropic Agent SDK is newer than the OpenAI one (the SDK GA'd
mid-2026); the adapter pins against the latest stable's known
surface and degrades gracefully if internal method names drift.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.openai_agents import (
    _TOOL_DISPATCH_CANDIDATES,
    _install_attr,
    _wrap_run_method,
    _wrap_tool_dispatcher,
)

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.anthropic_agent")

_INSTRUMENTED_MARKER = "_inkfoot_anthropic_agent_instrumentation"


class _AnthropicAgentInstrumentation:
    """Teardown handle returned by :meth:`AnthropicAgentAdapter.instrument`."""

    def __init__(
        self, target: Any, restorers: list[Callable[[], None]]
    ) -> None:
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


class AnthropicAgentAdapter:
    """Pattern-C adapter for Anthropic's Agent SDK."""

    name = "anthropic_agent"

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
            wrapped = _wrap_run_method(original, task=task or "anthropic_agent")
            _install_attr(target, method_name, wrapped, restorers)

        for method_name in _TOOL_DISPATCH_CANDIDATES:
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = _wrap_tool_dispatcher(original)
            _install_attr(target, method_name, wrapped, restorers)

        instrumentation = _AnthropicAgentInstrumentation(target, restorers)
        try:
            setattr(target, _INSTRUMENTED_MARKER, instrumentation)
        except (AttributeError, TypeError):
            pass

        try:
            AdapterRegistry.set_active(self)
        except Exception:  # pragma: no cover
            _LOG.warning("activate failed", exc_info=True)

        return instrumentation

    def supported_policies(self) -> set[type["Policy"]]:
        """Same Phase 1 posture as the OpenAI Agents adapter — empty
        set lets the Phase-0 observation policies through the
        pattern-fallback path; Phase 2 will enumerate modification
        policies here."""
        return set()

    def shutdown(self) -> None:
        AdapterRegistry.clear_active()


_default_adapter = AnthropicAgentAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _AnthropicAgentInstrumentation:
    """User-facing convenience — equivalent to
    ``AnthropicAgentAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)
