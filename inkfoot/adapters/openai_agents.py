"""OpenAI Agents SDK adapter — Pattern-C wrap for ``Agent.run`` plus
the tool-dispatch layer.

OpenAI Agents adapter:

* Wrap ``Agent.run`` and ``Agent.run_async`` so the loop is scoped
  under an :func:`inkfoot.agent_run`.
* Emit a ``tool_dispatched`` event per tool invocation with
  ``tool_name``, ``tool_args_hash``, and ``dispatch_latency_ms``.
* Declare capability so future modification policies that target
  ``Agent.run`` register cleanly.

Duck-typed against the SDK — no module-load-time import. The
adapter accepts either the ``Agent`` class (wraps all instances)
or a single instance.

The wrapping primitives (``wrap_run_method``, ``wrap_tool_dispatcher``,
``install_attr``, ``TOOL_DISPATCH_CANDIDATES``) live in
:mod:`inkfoot.adapters._shared` so the Anthropic Agent adapter can
share them without reaching for cross-module private names.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters._shared import (
    TOOL_DISPATCH_CANDIDATES,
    install_attr,
    stable_args_hash,
    wrap_run_method,
    wrap_tool_dispatcher,
)

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.openai_agents")

_INSTRUMENTED_MARKER = "_inkfoot_openai_agents_instrumentation"


# Backwards-compatible re-export so external callers / older imports
# continue to resolve. New code should import from
# :mod:`inkfoot.adapters._shared` directly.
_stable_args_hash = stable_args_hash
_TOOL_DISPATCH_CANDIDATES = TOOL_DISPATCH_CANDIDATES


class _OpenAIAgentsInstrumentation:
    """Teardown handle returned by :meth:`OpenAIAgentsAdapter.instrument`.

    ``shutdown()`` unwraps the entry-point + tool-dispatch patches
    on the agent instance. The adapter-level active-pointer in
    :data:`~inkfoot.adapters._registry.AdapterRegistry` is handled
    by the adapter's install-count book-keeping — when the
    last live instrumentation shuts down,
    the active pointer clears automatically. Operators who want to
    force an early deactivation can call
    :meth:`OpenAIAgentsAdapter.shutdown` directly.
    """

    def __init__(
        self,
        adapter: "OpenAIAgentsAdapter",
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


class OpenAIAgentsAdapter:
    """Pattern-C adapter for the OpenAI Agents SDK."""

    name = "openai_agents"

    def __init__(self) -> None:
        # Install count — incremented on each ``instrument()`` call
        # that lands a new instrumentation handle (idempotent
        # re-instrument doesn't bump the count). The active-pointer
        # in :data:`AdapterRegistry` clears when this hits zero.
        self._install_count = 0

    def detect(self) -> bool:
        try:
            import agents  # noqa: F401, PLC0415  — pip name: openai-agents
        except ImportError:
            try:
                import openai_agents  # noqa: F401, PLC0415  — legacy
            except ImportError:
                return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _OpenAIAgentsInstrumentation:
        """Wrap ``target`` (an ``Agent`` class or instance) so its
        ``run`` / ``run_async`` calls scope an :func:`inkfoot.agent_run`
        and its tool dispatch emits ``tool_dispatched`` events.
        """
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _OpenAIAgentsInstrumentation):
            return existing

        restorers: list[Callable[[], None]] = []

        for method_name in ("run", "run_async"):
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_run_method(original, task=task or "openai_agents")
            install_attr(target, method_name, wrapped, restorers)

        for method_name in TOOL_DISPATCH_CANDIDATES:
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_tool_dispatcher(original)
            install_attr(target, method_name, wrapped, restorers)

        instrumentation = _OpenAIAgentsInstrumentation(self, target, restorers)
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
        """Called by an instrumentation's ``shutdown()`` to decrement
        the install count. When the count reaches zero, the adapter
        auto-clears its active-pointer slot — so a user who only
        calls ``inst.shutdown()`` doesn't leave the registry
        pointing at a "dead" adapter.
        """
        if self._install_count > 0:
            self._install_count -= 1
        if self._install_count == 0:
            active = AdapterRegistry.get_active()
            if active is self:
                AdapterRegistry.clear_active()

    def supported_policies(self) -> set[type["Policy"]]:
        """Modification policies this adapter knows how to wire. The
        observation policies don't need enumerating — the
        pattern-fallback path in :func:`register_policies` accepts
        them regardless because they support every pattern."""
        from inkfoot.policy import CheapSummariser, LazyToolExposure  # noqa: PLC0415

        return {LazyToolExposure, CheapSummariser}

    def shutdown(self) -> None:
        """Force the adapter to deactivate immediately, regardless of
        how many live instrumentations exist. Use sparingly — the
        per-instrumentation ``shutdown()`` already auto-deactivates
        on the last release."""
        AdapterRegistry.clear_active()
        self._install_count = 0


_default_adapter = OpenAIAgentsAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _OpenAIAgentsInstrumentation:
    """User-facing convenience — equivalent to
    ``OpenAIAgentsAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)
