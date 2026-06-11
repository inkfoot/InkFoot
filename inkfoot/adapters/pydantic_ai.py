"""Pydantic AI adapter — Pattern-C wrap for ``Agent.run`` /
``Agent.run_sync`` plus the registered-tool layer.

Pydantic AI adapter:

* Wrap ``Agent.run`` (async) and ``Agent.run_sync`` (sync) so each
  agent loop is scoped under an :func:`inkfoot.agent_run`.
* Emit a ``tool_dispatched`` event per tool invocation with
  ``tool_name``, ``tool_args_hash``, and ``dispatch_latency_ms`` —
  together with the ``llm_call`` events the provider shim records
  underneath, that gives one event per agent step.
* Declare capability so modification policies register cleanly.

Duck-typed against the SDK — no module-load-time import. The adapter
accepts an ``Agent`` instance (the common case) or anything exposing
the same entry-point surface.

Tool dispatch is intercepted at exactly one of two layers:

* Pydantic AI's registered-tool registry (``Agent._function_tools``,
  a ``name → Tool`` mapping whose ``Tool.run`` executes one call) —
  preferred, because it is the layer real SDK builds expose.
* Failing that, the generic dispatch-method probe shared with the
  other agent-SDK adapters (``_call_tool`` / ``dispatch_tool`` / ...).

The layers are never wrapped together: a dispatch method typically
routes through ``Tool.run``, so wrapping both would emit two
``tool_dispatched`` events per call. Internal names drift between
SDK builds; when neither layer is found the adapter still scopes
runs correctly and simply emits no ``tool_dispatched`` events.

``Agent.run_stream`` is *not* wrapped: it returns an async context
manager rather than a coroutine, so a naive wrap would end the run
before the stream is consumed. Streaming calls still attribute
correctly when the caller wraps them in their own
:func:`inkfoot.agent_run` scope.
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

_LOG = logging.getLogger("inkfoot.adapters.pydantic_ai")

_INSTRUMENTED_MARKER = "_inkfoot_pydantic_ai_instrumentation"

# Attribute names under which Pydantic AI builds keep the registered
# tool mapping (``name → Tool``). Probed in order; first dict wins.
_TOOL_REGISTRY_CANDIDATES: tuple[str, ...] = (
    "_function_tools",
    "_function_toolset",
)


class _PydanticAIInstrumentation:
    """Teardown handle returned by :meth:`PydanticAIAdapter.instrument`.

    ``shutdown()`` unwraps the entry-point + tool patches on the
    agent. The adapter's install-count book-keeping clears the
    active-adapter pointer in
    :data:`~inkfoot.adapters._registry.AdapterRegistry` when the last
    live instrumentation shuts down; operators who need an immediate
    global deactivation can call :meth:`PydanticAIAdapter.shutdown`.
    """

    def __init__(
        self,
        adapter: "PydanticAIAdapter",
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


class PydanticAIAdapter:
    """Pattern-C adapter for Pydantic AI."""

    name = "pydantic_ai"

    def __init__(self) -> None:
        # Install count — incremented per new instrumentation handle
        # (idempotent re-instrument doesn't bump it). The
        # active-pointer in :data:`AdapterRegistry` clears when this
        # hits zero.
        self._install_count = 0

    def detect(self) -> bool:
        try:
            import pydantic_ai  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _PydanticAIInstrumentation:
        """Wrap ``target`` (a Pydantic AI ``Agent`` instance) so its
        ``run`` / ``run_sync`` calls scope an :func:`inkfoot.agent_run`
        and its tool invocations emit ``tool_dispatched`` events.
        """
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _PydanticAIInstrumentation):
            return existing

        restorers: list[Callable[[], None]] = []

        for method_name in ("run", "run_sync"):
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_run_method(original, task=task or "pydantic_ai")
            install_attr(target, method_name, wrapped, restorers)

        # Registry first; the generic probe only as fallback. A
        # dispatch method typically routes through ``Tool.run``, so
        # wrapping both layers would double-emit ``tool_dispatched``.
        if not self._wrap_registered_tools(target, restorers):
            for method_name in TOOL_DISPATCH_CANDIDATES:
                original = getattr(target, method_name, None)
                if original is None or not callable(original):
                    continue
                wrapped = wrap_tool_dispatcher(original)
                install_attr(target, method_name, wrapped, restorers)

        instrumentation = _PydanticAIInstrumentation(self, target, restorers)
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

    @staticmethod
    def _wrap_registered_tools(
        target: Any, restorers: list[Callable[[], None]]
    ) -> bool:
        """Wrap ``Tool.run`` on every tool in the agent's registry so
        each invocation emits a ``tool_dispatched`` event. Tolerant of
        missing/foreign registry shapes — wraps what it recognises and
        skips the rest. Returns ``True`` when a registry mapping was
        found, which tells the caller to skip the generic
        dispatch-method probe (preventing a double emit)."""
        for registry_name in _TOOL_REGISTRY_CANDIDATES:
            holder = getattr(target, registry_name, None)
            # Either a bare ``name → Tool`` dict or a toolset object
            # carrying the same mapping on ``.tools``.
            registry = (
                holder
                if isinstance(holder, dict)
                else getattr(holder, "tools", None)
            )
            if not isinstance(registry, dict):
                continue
            for tool in registry.values():
                run_method = getattr(tool, "run", None)
                if not callable(run_method):
                    continue
                install_attr(
                    tool, "run", wrap_tool_dispatcher(run_method), restorers
                )
            return True
        return False

    def _release_install(self) -> None:
        """Decrement the install count; auto-clear the active pointer
        at zero so a user who only calls ``inst.shutdown()`` doesn't
        leave the registry pointing at a "dead" adapter."""
        if self._install_count > 0:
            self._install_count -= 1
        if self._install_count == 0:
            active = AdapterRegistry.get_active()
            if active is self:
                AdapterRegistry.clear_active()

    def supported_policies(self) -> set[type["Policy"]]:
        """Modification policies this adapter knows how to wire — the
        same surface as the other agent-SDK adapters. Observation
        policies pass the pattern-fallback path in
        :func:`register_policies` without being enumerated here."""
        from inkfoot.policy import CheapSummariser, LazyToolExposure  # noqa: PLC0415

        return {LazyToolExposure, CheapSummariser}

    def shutdown(self) -> None:
        """Force-deactivate immediately, regardless of live
        instrumentation count. Usually unnecessary — the per-
        instrumentation ``shutdown()`` auto-deactivates on the last
        release."""
        AdapterRegistry.clear_active()
        self._install_count = 0


_default_adapter = PydanticAIAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _PydanticAIInstrumentation:
    """User-facing convenience — equivalent to
    ``PydanticAIAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)
