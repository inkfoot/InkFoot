"""Process-local active-run pointer used by the SDK shims.

The shim needs to know "which run am I emitting this event into?"
on every call. In the current implementation there are three ways the active run gets
set:

1. **Explicit** — :func:`inkfoot.agent_run` decorator / context
   manager will set + clear via :func:`_set_current_run`.
2. **Ambient** — when no run is active and the shim fires anyway,
   it lazily creates an "ambient" run (one per process, reused
   across calls) so the event isn't dropped. The run lifecycle replaces this
   path with explicit scoping in production code.
3. **Tests** — tests can drive the pointer directly via
   :func:`_set_current_run` / :func:`_clear_current_run`.

We use :class:`contextvars.ContextVar` rather than ``threading.local``
because async SDK calls run inside a task; ContextVars propagate
correctly across task boundaries (PEP 567) while thread-local would
either leak between tasks or vanish.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.run import InMemoryRunState
    from inkfoot.storage import Storage


_LOG = logging.getLogger("inkfoot.run_context")


# The current run id. ``None`` when no run is active; an ambient run
# is created the first time the shim fires under a None context.
_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "inkfoot_current_run_id", default=None
)


# In-memory state map keyed by run_id. Holds the InMemoryRunState
# instance the translators mutate (stable_system_prefix etc.). Kept
# *here* rather than on Storage deliberately: it's process-local,
# never persisted.
_run_states: dict[str, "InMemoryRunState"] = {}
_run_states_lock = threading.Lock()


def current_run_id() -> Optional[str]:
    """Return the active run id, or ``None`` if none is set. Read by
    the shim on every call."""
    return _current_run_id.get()


def _set_current_run(run_id: str) -> contextvars.Token:
    """Set the active run id; returns the token the caller must hold
    to restore the prior value via :func:`_reset_current_run`."""
    return _current_run_id.set(run_id)


def _reset_current_run(token: contextvars.Token) -> None:
    """Restore the prior run id (matching the most recent
    :func:`_set_current_run`)."""
    _current_run_id.reset(token)


def _clear_current_run() -> None:
    """Reset the run id to ``None`` unconditionally. Tests use this
    between cases."""
    _current_run_id.set(None)


def get_or_create_run_state(run_id: str) -> "InMemoryRunState":
    """Return the :class:`InMemoryRunState` for ``run_id``, creating
    one on first access. Thread-safe."""
    from inkfoot.run import InMemoryRunState

    with _run_states_lock:
        state = _run_states.get(run_id)
        if state is None:
            state = InMemoryRunState()
            _run_states[run_id] = state
        return state


def _drop_run_state(run_id: str) -> None:
    """Tests + ``end_run`` use this to release the in-memory state
    once the run is done. Idempotent."""
    with _run_states_lock:
        _run_states.pop(run_id, None)


# --------------------------------------------------------------------
# Ambient-run lazy creation
# --------------------------------------------------------------------

# One ambient run per process. Reused across calls until the process
# exits. ``agent_run`` will replace this in production code.
_AMBIENT_RUN_LOCK = threading.Lock()
_ambient_run_id: Optional[str] = None


def ensure_active_run(storage: "Storage", *, now_ms: int) -> str:
    """Return an active run id, creating an ambient run lazily if
    none is set on the current context.

    Idempotent: subsequent calls without an explicit run set return
    the same ambient run id. Tests that want isolation should call
    :func:`_reset_ambient_run`.
    """
    explicit = current_run_id()
    if explicit is not None:
        return explicit

    global _ambient_run_id
    with _AMBIENT_RUN_LOCK:
        if _ambient_run_id is None:
            from ulid import ULID  # python-ulid

            ambient = f"ambient-{str(ULID())}"
            storage.start_run(
                run_id=ambient,
                task="ambient",
                agent_kind="ambient",
                started_at=now_ms,
            )
            _ambient_run_id = ambient
        return _ambient_run_id


def _reset_ambient_run() -> None:
    """Tests: clear the cached ambient run id so a subsequent
    :func:`ensure_active_run` creates a fresh one. Does **not**
    touch storage — the previous ambient run row stays in the DB
    until the process exits."""
    global _ambient_run_id
    with _AMBIENT_RUN_LOCK:
        _ambient_run_id = None
