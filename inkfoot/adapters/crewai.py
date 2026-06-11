"""CrewAI adapter — Pattern-C wrap for ``Crew.kickoff`` /
``Crew.kickoff_async`` plus multi-agent attribution scopes.

CrewAI adapter:

* Wrap ``Crew.kickoff`` (sync) and ``Crew.kickoff_async`` (async) so
  each crew execution is scoped under an :func:`inkfoot.agent_run`.
* Scope every agent's ``execute_task`` so LLM calls made while that
  agent is working carry ``metadata["agent_name"]`` (``Agent.name``,
  falling back to ``Agent.role``).
* Scope every CrewAI task's ``execute_sync`` / ``execute_async`` /
  ``execute`` the same way for ``metadata["task_name"]``
  (``Task.name``, falling back to a whitespace-collapsed slice of
  the description).

The scopes write ``agent_name`` / ``task_name`` onto the live run's
:class:`~inkfoot.run.InMemoryRunState`; the provider translators
stamp them onto ``NeutralCall.metadata``. No new event kinds — the
existing ``llm_call`` events simply gain attribution, which is what
``inkfoot report --group-by metadata.agent_name`` slices on.

Duck-typed against the SDK — no module-load-time import. The
adapter accepts a ``Crew`` instance (the common case) or anything
exposing the same surface.

CrewAI's ``Crew`` / ``Agent`` / ``Task`` are pydantic models that
reject unknown public attribute assignment, so wrappers are
installed with an ``object.__setattr__`` fallback (instance
``__dict__`` shadowing) rather than plain ``setattr`` alone.

Capability surface: **observation-only**. To be precise: CrewAI's
LLM calls *do* traverse the instrumented provider shim (that is how
the attribution metadata lands), so a request-level rewrite seam
physically exists. What CrewAI doesn't expose is the stable
per-turn context the modification policies need — the
framework-owned tool registry and turn boundaries — and it
assembles each request from internal state that an external rewrite
could silently desync. So request-modification policies are not
offered. Observation policies work as on every adapter — they pass
the pattern-fallback path in :func:`register_policies` without
adapter enumeration.

Not wrapped:

* ``Crew.kickoff_for_each`` (and ``_async``) — they execute on
  *copies* of the crew, which instance-level instrumentation can't
  see. Instrument each copy, or run the loop inside your own
  :func:`inkfoot.agent_run` scope.
* Tool dispatch — CrewAI routes tool execution through internal
  helpers that vary by build, so this adapter emits no
  ``tool_dispatched`` events. LLM-call events still attribute
  fully.

Parallel task execution (``Task.execute_async`` runs in a worker
thread on some builds) may execute outside the run's context; the
default sequential process attributes fully.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters._shared import wrap_run_method

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.crewai")

_INSTRUMENTED_MARKER = "_inkfoot_crewai_instrumentation"

# Execute-method names probed on each agent / CrewAI-task object.
# ``execute_task`` / ``execute_sync`` have been stable for a while;
# the older spellings are kept for cheap compatibility.
_AGENT_EXECUTE_CANDIDATES: tuple[str, ...] = ("execute_task",)
_TASK_EXECUTE_CANDIDATES: tuple[str, ...] = (
    "execute_sync",
    "execute_async",
    "execute",
)

# Cap for task labels derived from ``Task.description`` —
# descriptions are prose paragraphs, and reports want a short
# bucket key. Tasks needing exact labels should set ``Task.name``.
_TASK_LABEL_MAX_CHARS = 80


def _agent_label(agent: Any) -> Optional[str]:
    """``Agent.name`` when set, else ``Agent.role`` (always present
    on real CrewAI agents). ``None`` when neither yields a usable
    string — the agent is then left unwrapped."""
    for attr in ("name", "role"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _task_label(crew_task: Any) -> Optional[str]:
    """``Task.name`` when set, else the description collapsed to one
    line and truncated. ``None`` when neither yields a usable
    string."""
    name = getattr(crew_task, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    description = getattr(crew_task, "description", None)
    if isinstance(description, str) and description.strip():
        return " ".join(description.split())[:_TASK_LABEL_MAX_CHARS]
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _crew_agents(target: Any) -> list[Any]:
    """The crew's agents plus, when present, the hierarchical-process
    manager agent (kept on ``Crew.manager_agent``, not in
    ``Crew.agents``)."""
    agents = _as_list(getattr(target, "agents", None))
    manager = getattr(target, "manager_agent", None)
    if manager is not None:
        agents.append(manager)
    return agents


class _AttributionScope:
    """Sets one attribution field (``agent_name`` / ``task_name``)
    on the live run's :class:`InMemoryRunState` for the duration of
    a framework call, restoring the prior value on exit so nested
    scopes (a manager delegating to a worker agent) unwind
    correctly.

    No-op when no run is active — execution outside the wrapped
    kickoff (and outside a user ``agent_run`` block) has no state to
    stamp.

    The field lives on the (single) run state, so attribution
    assumes scopes nest rather than interleave — true for CrewAI's
    default sequential process, same trade-off as ``node_name``.
    Concurrent same-run agents (async fan-out) would overwrite each
    other's stamp.
    """

    def __init__(self, attr: str, value: str) -> None:
        self._attr = attr
        self._value = value
        self._prior: Optional[str] = None
        self._run_id: Optional[str] = None
        self._has_run = False

    def __enter__(self) -> "_AttributionScope":
        from inkfoot._run_context import (  # noqa: PLC0415
            current_run_id,
            get_or_create_run_state,
        )

        self._run_id = current_run_id()
        if self._run_id is None:
            return self
        self._has_run = True
        state = get_or_create_run_state(self._run_id)
        self._prior = getattr(state, self._attr, None)
        setattr(state, self._attr, self._value)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._has_run or self._run_id is None:
            return
        from inkfoot._run_context import (  # noqa: PLC0415
            get_or_create_run_state,
        )

        state = get_or_create_run_state(self._run_id)
        setattr(state, self._attr, self._prior)


def _wrap_scoped(
    method: Callable[..., Any], *, attr: str, value: str
) -> Callable[..., Any]:
    """Wrap an execute method (sync or async) so it runs under an
    :class:`_AttributionScope`."""
    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _AttributionScope(attr, value):
                return await method(*args, **kwargs)

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _AttributionScope(attr, value):
            return method(*args, **kwargs)

    return wrapper


def _install_attr(
    target: Any,
    name: str,
    wrapped: Any,
    restorers: list[Callable[[], None]],
) -> None:
    """Like :func:`inkfoot.adapters._shared.install_attr`, plus a
    pydantic escape hatch: CrewAI objects are pydantic models whose
    ``__setattr__`` raises ``ValueError`` for unknown public names,
    so a refused ``setattr`` retries via ``object.__setattr__``
    (the instance ``__dict__`` entry then shadows the class method,
    which is exactly the install the plain path performs)."""
    sentinel: Any = object()
    original = (
        target.__dict__.get(name, sentinel)
        if hasattr(target, "__dict__")
        else sentinel
    )
    try:
        setattr(target, name, wrapped)
    except (AttributeError, TypeError, ValueError):
        try:
            object.__setattr__(target, name, wrapped)
        except (AttributeError, TypeError):  # pragma: no cover
            return

    def _restore() -> None:
        if original is sentinel:
            try:
                delattr(target, name)
            except (AttributeError, TypeError, ValueError):
                try:
                    object.__delattr__(target, name)
                except AttributeError:  # pragma: no cover
                    pass
        else:
            try:
                setattr(target, name, original)
            except (AttributeError, TypeError, ValueError):
                try:
                    object.__setattr__(target, name, original)
                except (AttributeError, TypeError):  # pragma: no cover
                    pass

    restorers.append(_restore)


class _CrewAIInstrumentation:
    """Teardown handle returned by :meth:`CrewAIAdapter.instrument`.

    ``shutdown()`` unwraps the kickoff + agent + task patches. The
    adapter's install-count book-keeping clears the active-adapter
    pointer in
    :data:`~inkfoot.adapters._registry.AdapterRegistry` when the
    last live instrumentation shuts down; operators who need an
    immediate global deactivation can call
    :meth:`CrewAIAdapter.shutdown`.
    """

    def __init__(
        self,
        adapter: "CrewAIAdapter",
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


class CrewAIAdapter:
    """Pattern-C adapter for CrewAI."""

    name = "crewai"

    def __init__(self) -> None:
        # Install count — incremented per new instrumentation handle
        # (idempotent re-instrument doesn't bump it). The
        # active-pointer in :data:`AdapterRegistry` clears when this
        # hits zero.
        self._install_count = 0

    def detect(self) -> bool:
        try:
            import crewai  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _CrewAIInstrumentation:
        """Wrap ``target`` (a CrewAI ``Crew`` instance) so its
        ``kickoff`` / ``kickoff_async`` calls scope an
        :func:`inkfoot.agent_run` and every agent / CrewAI-task
        execution stamps multi-agent attribution onto the run state.

        ``task`` here is the *inkfoot* run label (defaults to
        ``"crewai"``) — not to be confused with CrewAI's own
        ``Task`` objects, which the adapter labels via
        ``metadata["task_name"]``.
        """
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _CrewAIInstrumentation):
            return existing

        restorers: list[Callable[[], None]] = []

        for method_name in ("kickoff", "kickoff_async"):
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = wrap_run_method(original, task=task or "crewai")
            _install_attr(target, method_name, wrapped, restorers)

        for agent in _crew_agents(target):
            label = _agent_label(agent)
            if not label:
                continue
            self._wrap_execute_methods(
                agent,
                _AGENT_EXECUTE_CANDIDATES,
                attr="agent_name",
                label=label,
                restorers=restorers,
            )

        for crew_task in _as_list(getattr(target, "tasks", None)):
            label = _task_label(crew_task)
            if not label:
                continue
            self._wrap_execute_methods(
                crew_task,
                _TASK_EXECUTE_CANDIDATES,
                attr="task_name",
                label=label,
                restorers=restorers,
            )

        instrumentation = _CrewAIInstrumentation(self, target, restorers)
        try:
            setattr(target, _INSTRUMENTED_MARKER, instrumentation)
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            AdapterRegistry.set_active(self)
        except Exception:  # pragma: no cover
            _LOG.warning("activate failed", exc_info=True)
        self._install_count += 1

        return instrumentation

    @staticmethod
    def _wrap_execute_methods(
        holder: Any,
        candidates: tuple[str, ...],
        *,
        attr: str,
        label: str,
        restorers: list[Callable[[], None]],
    ) -> None:
        for method_name in candidates:
            original = getattr(holder, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = _wrap_scoped(original, attr=attr, value=label)
            _install_attr(holder, method_name, wrapped, restorers)

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
        """Empty on purpose — this adapter is observation-only.
        CrewAI assembles provider requests internally with no stable
        request-rewrite seam for a modification policy to hook.
        Observation policies don't need enumerating: they pass the
        pattern-fallback path in :func:`register_policies`."""
        return set()

    def shutdown(self) -> None:
        """Force-deactivate immediately, regardless of live
        instrumentation count. Usually unnecessary — the per-
        instrumentation ``shutdown()`` auto-deactivates on the last
        release."""
        AdapterRegistry.clear_active()
        self._install_count = 0


_default_adapter = CrewAIAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _CrewAIInstrumentation:
    """User-facing convenience — equivalent to
    ``CrewAIAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)
