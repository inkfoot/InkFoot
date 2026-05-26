"""LangGraph adapter — the headline Pattern-C integration.

Per phase-1-explain §4.1.1 + the E1-S2 task list, this adapter:

1. Wraps ``StateGraph.invoke / ainvoke / stream / astream`` to scope
   an :func:`inkfoot.agent_run` around the entire graph execution.
2. Wraps every compiled node function so per-node attribution is
   possible — node entry sets ``InMemoryRunState.node_name`` and
   emits a ``node_enter`` event; node exit emits ``node_exit``.
3. Captures the graph's ``tools`` registry once at instrument time
   and exposes its fingerprint via ``InMemoryRunState.tools_fingerprint``.
4. The translator (see ``inkfoot.normalise._collect_runtime_metadata``)
   stamps both fields onto :attr:`NeutralCall.metadata` so
   ``inkfoot report --group-by node`` can slice by them.

The adapter is **duck-typed** against the LangGraph surface — it
never imports ``langgraph`` at module load time. ``detect()``
attempts the import; ``instrument()`` accepts anything that exposes
the entry-point methods (most usefully a ``CompiledStateGraph``).
This keeps tests fast and protects users on older LangGraph builds.

Idempotence is keyed on a private ``_inkfoot_instrumentation``
attribute stamped onto the graph instance. A second
``instrument(graph)`` call returns the same handle.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters._shared import install_attr

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.langgraph")

# Sentinel marker — stamped on the graph instance so a second
# ``instrument()`` call short-circuits.
_INSTRUMENTED_MARKER = "_inkfoot_instrumentation"

# Length of the tools fingerprint (hex chars). 16 chars = 64 bits of
# hash, plenty for a "same tools registry as last call" check
# without bloating the metadata dict.
_FINGERPRINT_LEN = 16


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_tool_signature(tool: Any) -> dict[str, Any]:
    """Reduce a tool definition to a JSON-stable shape so the
    fingerprint stays the same across runs that pass the same
    semantic tools.

    LangGraph tools are usually one of:
      * A bare callable (function or :class:`StructuredTool`).
      * A dict with ``name``, ``description``, ``args_schema`` /
        ``parameters``.
      * A ``BaseTool``-style object exposing ``name`` /
        ``description`` / ``args_schema`` attributes.

    We snapshot only fields that affect *what the tool is*, not
    runtime state. Unknown shapes fall back to ``repr(tool)`` so the
    fingerprint stays stable across calls within a process.
    """
    if isinstance(tool, dict):
        name = tool.get("name") or tool.get("function", {}).get("name")
        description = tool.get("description") or tool.get(
            "function", {}
        ).get("description")
        schema = (
            tool.get("args_schema")
            or tool.get("parameters")
            or tool.get("function", {}).get("parameters")
        )
        return {
            "name": str(name or ""),
            "description": str(description or ""),
            "schema": _stringify_schema(schema),
        }

    name = getattr(tool, "name", None)
    description = getattr(tool, "description", None)
    schema = getattr(tool, "args_schema", None) or getattr(
        tool, "parameters", None
    )
    if name is not None:
        return {
            "name": str(name),
            "description": str(description or ""),
            "schema": _stringify_schema(schema),
        }

    # Bare callable / unknown shape — fall back to qualname.
    qualname = getattr(tool, "__qualname__", None) or getattr(
        tool, "__name__", None
    ) or repr(tool)
    return {"name": str(qualname), "description": "", "schema": ""}


def _stringify_schema(schema: Any) -> str:
    """Pydantic models / dicts → a stable JSON string."""
    if schema is None:
        return ""
    if isinstance(schema, dict):
        try:
            return json.dumps(schema, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(schema)
    schema_method = getattr(schema, "model_json_schema", None) or getattr(
        schema, "schema", None
    )
    if callable(schema_method):
        try:
            return json.dumps(schema_method(), sort_keys=True, default=str)
        except Exception:  # pragma: no cover — defensive
            pass
    return repr(schema)


def _compute_tools_fingerprint(tools: Any) -> Optional[str]:
    """Hash the tools registry into a short hex fingerprint.

    Returns ``None`` when ``tools`` is missing or empty. The hash is
    SHA-256 truncated to :data:`_FINGERPRINT_LEN` hex characters —
    well below the birthday-bound for any realistic per-process tool
    count, and small enough to round-trip through metadata without
    bloating reports.
    """
    if tools is None:
        return None
    try:
        seq = list(tools)
    except TypeError:
        return None
    if not seq:
        return None
    signatures = [_canonical_tool_signature(t) for t in seq]
    blob = json.dumps(signatures, sort_keys=True, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()[:_FINGERPRINT_LEN]


# ----------------------------------------------------------------------
# Per-node wrapping
# ----------------------------------------------------------------------


class _NodeScope:
    """Context-manager-style helper that scopes a node execution
    under Inkfoot bookkeeping:

    * Stamps the prior ``node_name`` onto :class:`InMemoryRunState`
      so multiple LLM calls inside the node all carry the same
      metadata.
    * Emits ``node_enter`` / ``node_exit`` events.
    * Restores the previous ``node_name`` on exit so a parent node
      that calls a child node doesn't get the child's label stuck.
    """

    def __init__(self, node_name: str) -> None:
        self._node_name = node_name
        self._prior_node_name: Optional[str] = None
        self._run_id: Optional[str] = None
        self._has_run: bool = False

    def __enter__(self) -> "_NodeScope":
        from inkfoot._run_context import (  # noqa: PLC0415
            current_run_id,
            get_or_create_run_state,
        )

        self._run_id = current_run_id()
        if self._run_id is None:
            # Nothing to do — the entry-point wrapper installs an
            # ``agent_run`` so this only fires when a node executes
            # outside the wrapped entry (advanced usage). We don't
            # emit phantom events.
            return self

        self._has_run = True
        state = get_or_create_run_state(self._run_id)
        self._prior_node_name = state.node_name
        state.node_name = self._node_name
        _emit_lifecycle_event(
            run_id=self._run_id,
            kind="node_enter",
            payload={"node_name": self._node_name},
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._has_run or self._run_id is None:
            return
        from inkfoot._run_context import (  # noqa: PLC0415
            get_or_create_run_state,
        )

        _emit_lifecycle_event(
            run_id=self._run_id,
            kind="node_exit",
            payload={
                "node_name": self._node_name,
                "error": (
                    f"{exc_type.__name__}: {exc}"[:512] if exc_type else None
                ),
            },
        )
        state = get_or_create_run_state(self._run_id)
        state.node_name = self._prior_node_name


def _emit_lifecycle_event(
    *, run_id: str, kind: str, payload: dict[str, Any]
) -> None:
    """Write one event row. Mirrors the helper in
    ``_run_lifecycle._emit_event`` but is module-local to keep the
    adapter independent of import order quirks (the run-lifecycle
    module imports from here in the e2e test setup)."""
    from ulid import ULID

    from inkfoot._instrument import _STORAGE  # noqa: PLC0415
    from inkfoot.shims._emit import _next_sequence  # noqa: PLC0415

    storage = _STORAGE
    if storage is None:
        # No-op when called pre-instrument() (e.g. a test that drives
        # an adapter without booting the runtime).
        return
    try:
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind=kind,
            occurred_at=_now_ms(),
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(payload, default=str),
            capture_mode="metadata",
        )
    except Exception:  # pragma: no cover — defensive
        _LOG.warning(
            "adapters.langgraph: failed to emit %s event", kind, exc_info=True
        )


def _wrap_node(
    node: Callable[..., Any], node_name: str
) -> Callable[..., Any]:
    """Wrap a node callable (sync or async) so each invocation runs
    under a :class:`_NodeScope`."""
    if asyncio.iscoroutinefunction(node):

        @functools.wraps(node)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _NodeScope(node_name):
                return await node(*args, **kwargs)

        async_wrapper.__inkfoot_wrapped_node__ = node  # type: ignore[attr-defined]
        return async_wrapper

    @functools.wraps(node)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _NodeScope(node_name):
            return node(*args, **kwargs)

    wrapper.__inkfoot_wrapped_node__ = node  # type: ignore[attr-defined]
    return wrapper


# ----------------------------------------------------------------------
# Entry-point wrapping
# ----------------------------------------------------------------------


def _wrap_entry(
    method: Callable[..., Any],
    *,
    task: Optional[str],
    tools_fingerprint: Optional[str],
) -> Callable[..., Any]:
    """Wrap one of ``invoke / ainvoke / stream / astream`` so the
    call is scoped under an :func:`inkfoot.agent_run` block and the
    tools fingerprint is set on the run's :class:`InMemoryRunState`.

    Re-entrant: if an outer run is already active (e.g. the user
    wrapped the call in their own :func:`agent_run` block) we don't
    open a new one — just set the fingerprint on the existing run
    state.
    """
    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            from inkfoot._run_context import (  # noqa: PLC0415
                current_run_id,
                get_or_create_run_state,
            )

            outer = current_run_id()
            if outer is not None:
                _set_fp(outer, tools_fingerprint)
                return await method(*args, **kwargs)

            import inkfoot  # noqa: PLC0415

            async with inkfoot.agent_run(
                task=task, metadata={"agent_kind": "langgraph"}
            ):
                inner = current_run_id()
                if inner is not None:
                    _set_fp(inner, tools_fingerprint)
                return await method(*args, **kwargs)

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from inkfoot._run_context import (  # noqa: PLC0415
            current_run_id,
        )

        outer = current_run_id()
        if outer is not None:
            _set_fp(outer, tools_fingerprint)
            return method(*args, **kwargs)

        import inkfoot  # noqa: PLC0415

        with inkfoot.agent_run(
            task=task, metadata={"agent_kind": "langgraph"}
        ):
            inner = current_run_id()
            if inner is not None:
                _set_fp(inner, tools_fingerprint)
            return method(*args, **kwargs)

    return wrapper


def _set_fp(run_id: str, tools_fingerprint: Optional[str]) -> None:
    """Update :attr:`InMemoryRunState.tools_fingerprint` for a run.
    No-op when the fingerprint is falsy."""
    if not tools_fingerprint:
        return
    from inkfoot._run_context import (  # noqa: PLC0415
        get_or_create_run_state,
    )

    state = get_or_create_run_state(run_id)
    state.tools_fingerprint = tools_fingerprint


# ----------------------------------------------------------------------
# Instrumentation handle
# ----------------------------------------------------------------------


class _LangGraphInstrumentation:
    """Returned by :func:`instrument`. Holds the restore-callables so
    :meth:`shutdown` can fully reverse the monkey-patches.

    ``shutdown()`` also releases the adapter's install count — when
    the last live instrumentation goes away, the active-adapter
    pointer in :data:`~inkfoot.adapters._registry.AdapterRegistry`
    clears automatically. This was added under CL-E1 review Finding
    #4: previously a user calling ``inst.shutdown()`` would leave
    the registry pointing at a "dead" adapter, so subsequent
    ``register_policies()`` calls would consult its
    ``supported_policies()`` even though no instrumentation was
    still installed. Operators who want to force a global
    deactivation regardless of install count can call
    :meth:`LangGraphAdapter.shutdown` directly.
    """

    def __init__(
        self,
        adapter: "LangGraphAdapter",
        graph: Any,
        restorers: list[Callable[[], None]],
        *,
        tools_fingerprint: Optional[str],
    ) -> None:
        self._adapter = adapter
        self._graph = graph
        self._restorers = restorers
        self._tools_fingerprint = tools_fingerprint
        self._shutdown = False

    @property
    def tools_fingerprint(self) -> Optional[str]:
        return self._tools_fingerprint

    def shutdown(self) -> None:
        """Reverse the entry-point + node wrapping. Idempotent."""
        if self._shutdown:
            return
        for restorer in reversed(self._restorers):
            try:
                restorer()
            except Exception:  # pragma: no cover
                _LOG.warning(
                    "adapters.langgraph: restore step raised", exc_info=True
                )
        try:
            delattr(self._graph, _INSTRUMENTED_MARKER)
        except AttributeError:  # pragma: no cover
            pass
        self._shutdown = True
        self._adapter._release_install()


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------


class LangGraphAdapter:
    """Concrete :class:`~inkfoot.adapters.base.FrameworkAdapter` for
    LangGraph (LangChain's ``langgraph`` package)."""

    name = "langgraph"

    def __init__(self) -> None:
        # Install count — tracked so the adapter's active-pointer in
        # :data:`AdapterRegistry` auto-clears when the last live
        # instrumentation shuts down (CL-E1 review Finding #4).
        self._install_count = 0

    def detect(self) -> bool:
        try:
            import langgraph  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _LangGraphInstrumentation:
        """Wrap ``target`` (a ``StateGraph`` or ``CompiledStateGraph``
        instance) with Inkfoot per-node attribution.

        ``task`` is the value passed to the wrapping
        :func:`inkfoot.agent_run`. Defaults to ``"langgraph"`` so a
        report can still bucket by task without the user passing it.

        **Idempotence + mutation caveat** (CL-E1 review Finding #6):
        a second ``instrument(graph)`` call returns the *same*
        handle that the first call produced — keyed on the graph
        instance via :data:`_INSTRUMENTED_MARKER`. This is the right
        shape for the common "compile once, invoke many" pattern.
        However, if a caller adds a node to ``graph.nodes`` *after*
        instrumenting and re-calls ``instrument(graph)``, the new
        node will not be wrapped — the cached handle short-circuits
        the rewrap. The fix in that case is ``inst.shutdown()`` then
        re-``instrument(graph)``. LangGraph users typically compile
        the graph once and don't mutate it afterwards, so this is a
        documented edge case rather than a daily concern.
        """
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _LangGraphInstrumentation):
            return existing

        tools_attr = (
            getattr(target, "tools", None)
            or getattr(target, "_tools", None)
        )
        tools_fingerprint = _compute_tools_fingerprint(tools_attr)

        restorers: list[Callable[[], None]] = []

        # 1. Wrap entry-point methods. We assign onto the instance's
        # __dict__ so the wrapped attribute shadows the class-level
        # method without mutating the class — uninstrument is then
        # ``del instance.invoke`` (which re-exposes the class method).
        for entry_name in ("invoke", "ainvoke", "stream", "astream"):
            original = getattr(target, entry_name, None)
            if original is None or not callable(original):
                continue
            wrapped = _wrap_entry(
                original, task=task or "langgraph",
                tools_fingerprint=tools_fingerprint,
            )
            install_attr(target, entry_name, wrapped, restorers)

        # 2. Wrap each node function. The compiled-graph shape varies
        # across LangGraph versions; support the two common attribute
        # layouts:
        #   * ``target.nodes`` — dict-like mapping ``node_name → fn``.
        #   * ``target.builder.nodes`` — same shape on the builder
        #     side (some CompiledStateGraph variants).
        # We wrap both in place so however the graph dispatches at
        # runtime, it lands in our wrapper.
        for nodes_holder in _node_holders(target):
            for node_name, node in list(nodes_holder.items()):
                callable_part, wrapper_factory = _extract_callable(node)
                if callable_part is None:
                    continue
                wrapped_callable = _wrap_node(callable_part, node_name)
                replacement = wrapper_factory(wrapped_callable)
                nodes_holder[node_name] = replacement
                # Capture by default-arg so each lambda closes over the
                # right variables (Python late-binding hazard otherwise).
                restorers.append(
                    (
                        lambda h=nodes_holder, n=node_name, o=node: h.__setitem__(
                            n, o
                        )
                    )
                )

        instrumentation = _LangGraphInstrumentation(
            self,
            target,
            restorers,
            tools_fingerprint=tools_fingerprint,
        )
        # Stash a back-pointer so idempotent re-instrument returns
        # the same handle.
        try:
            setattr(target, _INSTRUMENTED_MARKER, instrumentation)
        except (AttributeError, TypeError):
            # Some compiled graphs are slotted / frozen. Idempotence
            # then degrades to "second call rewraps"; we still return
            # a valid handle for the caller's teardown.
            pass

        # Register + activate so policy capability checks consult this
        # adapter's surface for the rest of the process. Idempotent on
        # re-instrument.
        try:
            AdapterRegistry.set_active(self)
        except Exception:  # pragma: no cover — defensive
            _LOG.warning(
                "adapters.langgraph: failed to activate adapter", exc_info=True
            )
        self._install_count += 1

        return instrumentation

    def _release_install(self) -> None:
        """Called by an instrumentation's ``shutdown()`` to decrement
        the install count. When it reaches zero, the active-pointer
        auto-clears so subsequent ``register_policies()`` calls
        don't consult this adapter's ``supported_policies()`` after
        the last instrumentation has been torn down (CL-E1 review
        Finding #4)."""
        if self._install_count > 0:
            self._install_count -= 1
        if self._install_count == 0:
            active = AdapterRegistry.get_active()
            if active is self:
                AdapterRegistry.clear_active()

    def supported_policies(self) -> set[type["Policy"]]:
        """Phase 1 ships only observation policies — which all
        support every integration pattern via the policy class's
        ``SUPPORTED_PATTERNS`` set. Returning an empty set lets the
        pattern-fallback path in :func:`register_policies` accept
        them; Phase 2 will enumerate the modification policies the
        adapter knows how to wire."""
        return set()

    def shutdown(self) -> None:
        """Force the adapter to deactivate immediately, regardless of
        how many live instrumentations exist. Use sparingly — the
        per-instrumentation ``shutdown()`` already auto-deactivates
        on the last release (CL-E1 review Finding #4). Use this
        method when you need to clear the registry pointer without
        having a handle on the individual ``_LangGraphInstrumentation``
        objects (e.g. test teardown, force-rebind to a different
        framework)."""
        AdapterRegistry.clear_active()
        self._install_count = 0


# Module-singleton for the most common ``inkfoot.langgraph.instrument(graph)``
# entry path. Tests + advanced callers can instantiate their own.
_default_adapter = LangGraphAdapter()


def instrument(
    graph: Any,
    *,
    task: Optional[str] = None,
    **kwargs: Any,
) -> _LangGraphInstrumentation:
    """User-facing convenience — wraps ``graph`` with the default
    adapter instance. Equivalent to::

        LangGraphAdapter().instrument(graph, task=task)
    """
    return _default_adapter.instrument(graph, task=task, **kwargs)


# ----------------------------------------------------------------------
# LangGraph-specific helpers (the entry-point ``install_attr`` lives
# in :mod:`inkfoot.adapters._shared` since both Agent-SDK adapters
# share it).
# ----------------------------------------------------------------------


def _node_holders(target: Any) -> list[Any]:
    """Return every dict-like that holds the graph's nodes. Modern
    LangGraph keeps them on ``target.nodes``; some compiled-graph
    variants also expose ``target.builder.nodes``."""
    holders: list[Any] = []
    nodes = getattr(target, "nodes", None)
    if isinstance(nodes, dict):
        holders.append(nodes)
    builder = getattr(target, "builder", None)
    if builder is not None:
        builder_nodes = getattr(builder, "nodes", None)
        if isinstance(builder_nodes, dict) and builder_nodes is not nodes:
            holders.append(builder_nodes)
    return holders


def _extract_callable(
    node: Any,
) -> tuple[Optional[Callable[..., Any]], Callable[[Callable[..., Any]], Any]]:
    """LangGraph stores nodes as either:

      * a bare callable, or
      * a small wrapper object (e.g. ``RunnableLambda``,
        ``ToolNode``) with the underlying callable on
        ``.runnable`` / ``.func`` / ``.invoke``.

    Returns ``(callable, factory)``: ``callable`` is the function to
    wrap; ``factory(new_callable)`` produces the replacement that
    goes back into the nodes dict (preserving the wrapper object if
    one was there).
    """
    if callable(node) and not hasattr(node, "func") and not hasattr(node, "runnable"):
        return node, lambda new: new

    for attr in ("func", "runnable"):
        inner = getattr(node, attr, None)
        if callable(inner):
            def _factory(new: Callable[..., Any], attr=attr, node=node) -> Any:
                try:
                    setattr(node, attr, new)
                except (AttributeError, TypeError):
                    return new
                return node

            return inner, _factory

    if callable(node):
        return node, lambda new: new
    return None, lambda new: new
