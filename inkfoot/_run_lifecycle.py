"""Run-lifecycle API: ``@inkfoot.agent_run``, ``set_outcome``,
``tag``, ``tag_retrieval``, ``report_cost``.

The instrumentation layer already provides the ContextVar-based active-run pointer (see
``inkfoot/_run_context.py``) plus an ambient-run fallback for
SDK-only callers who never enter an explicit run. This module
layers the *explicit* run lifecycle on top: a context manager + a
decorator that:

1. Insert the ``running`` row via ``storage.start_run``.
2. Emit a ``run_start`` event.
3. Update the ContextVar so child shim calls see the new run.
4. On exit: emit ``run_end`` with status ``complete`` or ``error``
   and call ``storage.end_run``.

Nested runs are allowed; the inner run's ``parent_run_id`` is the
outer's id. The ContextVar token machinery handles correct
restoration on exit (including async tasks).

**Abandonment.** If the process exits before ``__exit__`` runs
(``kill -9`` after a successful ``start_run``), the run row is
left at ``status='running'``. The atexit hook from
``inkfoot._instrument.shutdown`` calls :func:`_mark_abandoned_runs`
so anything still ``running`` flips to ``status='error'`` with
``error_message='abandoned'``.

**State machine**:

  Created → Running → Complete   (no exception + outcome set)
                    → Errored    (exception)
                    → Abandoned  (process exit without exit)
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from ulid import ULID

from inkfoot._run_context import (
    _reset_current_run,
    _set_current_run,
    current_run_id,
    get_or_create_run_state,
)
from inkfoot.errors import InkfootError

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.storage import Storage


_LOG = logging.getLogger("inkfoot.run_lifecycle")

_VALID_OUTCOMES = frozenset({"success", "failure", "human_escalated"})


class NoActiveRun(InkfootError):
    """Raised when an API call (``set_outcome``, ``tag``,
    ``tag_retrieval``) is made outside an active ``agent_run``
    block. The remediation is to wrap the work in ``with
    inkfoot.agent_run(task="...")`` or to use the
    ``@inkfoot.agent_run(...)`` decorator."""


def _resolve_storage() -> "Storage":
    """Return the storage instance the active ``instrument()`` call
    is using. Raises ``InkfootError`` when the runtime hasn't been
    instrumented — agent_run requires storage."""
    from inkfoot._instrument import _STORAGE  # noqa: PLC0415

    if _STORAGE is None:
        raise InkfootError(
            "inkfoot.agent_run requires inkfoot.instrument() to be called "
            "first (storage isn't initialised). Call inkfoot.instrument() "
            "in your application startup."
        )
    return _STORAGE


def _now_ms() -> int:
    return int(time.time() * 1000)


def _next_sequence_for(run_id: str) -> int:
    """Allocate the next sequence number for ``run_id``, sharing the
    counter the shim uses. Run-lifecycle events
    (``run_start``, ``run_end``, ``outcome``, ``user_tag``) need
    sequence numbers too; reusing the shim's counter keeps
    ``ORDER BY sequence`` consistent across all event kinds."""
    from inkfoot.shims._emit import _next_sequence  # noqa: PLC0415

    return _next_sequence(run_id)


def _emit_event(
    *,
    run_id: str,
    kind: str,
    payload: dict[str, Any],
    storage: Optional["Storage"] = None,
) -> None:
    """Write one event row. Run-lifecycle events never carry replay
    content — we always pass ``capture_mode='metadata'`` regardless
    of the global setting, because run-lifecycle metadata is part of
    the schema we always show in reports."""
    store = storage or _resolve_storage()
    store.insert_event(
        event_id=str(ULID()),
        run_id=run_id,
        kind=kind,
        occurred_at=_now_ms(),
        sequence=_next_sequence_for(run_id),
        payload_json=json.dumps(payload, default=str),
        capture_mode="metadata",
    )


# ----------------------------------------------------------------------
# Public API: set_outcome / tag / tag_retrieval / report_cost
# ----------------------------------------------------------------------


def set_outcome(
    outcome: str, quality_score: Optional[float] = None
) -> None:
    """Mark the active run's outcome. Emits an ``outcome`` event the
    aggregator picks up into ``runs.outcome`` (and
    ``runs.quality_score``).

    ``outcome`` must be one of ``"success"``, ``"failure"``, or
    ``"human_escalated"``. ``quality_score`` is a float in ``[0, 1]``
    or ``None``; values outside the range are rejected at the
    boundary so a report renderer's "0.94/1.00" formatting stays
    well-defined.

    Raises :class:`NoActiveRun` if called outside an ``agent_run``
    block — the message names ``@inkfoot.agent_run`` as the fix.
    """
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"set_outcome: outcome must be one of {sorted(_VALID_OUTCOMES)}, "
            f"got {outcome!r}"
        )
    if quality_score is not None:
        if not isinstance(quality_score, (int, float)) or isinstance(
            quality_score, bool
        ):
            raise TypeError(
                f"quality_score must be float or None, got "
                f"{type(quality_score).__name__}"
            )
        if not 0.0 <= float(quality_score) <= 1.0:
            raise ValueError(
                f"quality_score must be in [0, 1], got {quality_score}"
            )

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.set_outcome() called outside an active run. "
            "Wrap the work in `with inkfoot.agent_run(task='...'):` or "
            "decorate the function with @inkfoot.agent_run(task='...')."
        )
    _emit_event(
        run_id=run_id,
        kind="outcome",
        payload={"outcome": outcome, "quality_score": quality_score},
    )
    # Advisory: compare the task's trailing success rate to its
    # contract floor and emit a violation event if it has slipped.
    # Never raises into the caller.
    from inkfoot.contracts import runtime as _contract_runtime

    _contract_runtime.notify_outcome(run_id, outcome)


# JSON-scalar value sentinels — what ``tag`` accepts.
_SCALAR_TYPES = (str, int, float, bool, type(None))


def tag(key: str, value: Any) -> None:
    """Attach a (key, value) tag to the active run. Emits a
    ``user_tag`` event. Value must be a JSON-serialisable scalar
    (``str``, ``int``, ``float``, ``bool``, ``None``) — complex
    objects are rejected to keep the event payload small and
    grep-able in reports.

    Raises :class:`NoActiveRun` when called outside an active run.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("tag: key must be a non-empty string")
    if not isinstance(value, _SCALAR_TYPES):
        raise TypeError(
            f"tag: value must be a JSON-scalar (str, int, float, bool, None), "
            f"got {type(value).__name__}"
        )
    # ``isinstance(True, int)`` is True; we don't need to special-case
    # bool here because bool *is* a JSON scalar.

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.tag() called outside an active run. Wrap the work in "
            "`with inkfoot.agent_run(task='...'):` first."
        )
    _emit_event(
        run_id=run_id,
        kind="user_tag",
        payload={"key": key, "value": value},
    )


def tag_node(name: Optional[str]) -> None:
    """Mark the current run's "active node" name (Pattern B's manual
    analogue of LangGraph per-node attribution — framework metadata contract).

    The next LLM call's translator reads
    ``InMemoryRunState.node_name`` and attaches it to
    ``NeutralCall.metadata["node_name"]``. The value stays set until
    overwritten by another :func:`tag_node` call (or cleared by
    passing ``None``), so multi-call nodes don't need to re-tag for
    every call.

    Pattern B example::

        with inkfoot.agent_run(task="customer-support-triage"):
            inkfoot.tag_node("retrieval")
            chunks = retrieve(...)
            inkfoot.tag_node("synthesis")
            answer = synthesise(chunks)

    Empty strings are treated as a clear (``None``-equivalent) so
    callers don't accidentally set node_name="" — that would surface
    in reports as a literal blank label.

    Raises :class:`NoActiveRun` outside an active run.
    """
    if name is not None and not isinstance(name, str):
        raise TypeError(
            f"tag_node: name must be str or None, got {type(name).__name__}"
        )

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.tag_node() called outside an active run. Wrap the "
            "work in `with inkfoot.agent_run(task='...'):` first."
        )

    state = get_or_create_run_state(run_id)
    cleaned: Optional[str] = name if name else None
    state.node_name = cleaned


def checkpoint(label: str) -> None:
    """Emit a ``checkpoint`` event so reports can show time spent
    between checkpoints.

    Useful for raw-SDK / Pattern B agents that want to mark workflow
    boundaries without committing to LangGraph node names. Two
    successive checkpoints in the event stream let a report
    subtract the second's ``occurred_at`` from the first's to show
    "X ms between 'after-vector-search' and 'after-synthesis'".

    Empty / whitespace-only labels are rejected — they'd render as a
    blank row in reports.

    Raises :class:`NoActiveRun` outside an active run.
    """
    if not isinstance(label, str):
        raise TypeError(
            f"checkpoint: label must be str, got {type(label).__name__}"
        )
    cleaned = label.strip()
    if not cleaned:
        raise ValueError("checkpoint: label must be a non-empty string")

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.checkpoint() called outside an active run. Wrap "
            "the work in `with inkfoot.agent_run(task='...'):` first."
        )

    _emit_event(
        run_id=run_id,
        kind="checkpoint",
        payload={"label": cleaned},
    )


def tag_retrieval(text: str) -> None:
    """Mark ``text`` as retrieved context for the *next* LLM call.

    The text is tokenised against a default encoding (``o200k_base``,
    matching the tokenisers module's Anthropic fallback) and the
    count is added to ``InMemoryRunState.pending_retrieved_context_tokens``.
    The next ``AnthropicTranslator.translate`` (or OpenAI translator)
    call reads + resets the pending counter, lifting the tokens into
    that call's ``retrieved_context_tokens`` ledger field.

    Why count here vs. on the call: tag_retrieval can be called
    multiple times before a single LLM call (a retriever returns N
    chunks). Summing them on the in-memory state and lifting once
    keeps the ledger faithful to "this LLM call saw these
    retrieved-context tokens", regardless of how many tag_retrieval
    invocations produced them.

    Raises :class:`NoActiveRun` outside an active run.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"tag_retrieval: text must be str, got {type(text).__name__}"
        )

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.tag_retrieval() called outside an active run. "
            "Wrap the work in `with inkfoot.agent_run(task='...'):` first."
        )

    if text == "":
        return  # no-op for empty marker

    # Token count via the same fallback encoding the translators use
    # for Anthropic. A future per-call model tokeniser would refine
    # this; the current implementation is consistent enough to feed retrieved_context
    # attribution.
    import tiktoken  # noqa: PLC0415

    encoding = tiktoken.get_encoding("o200k_base")
    count = len(encoding.encode(text))

    state = get_or_create_run_state(run_id)
    state.pending_retrieved_context_tokens += count


def report_cost():
    """Return the current run's accumulated cost as a
    :class:`decimal.Decimal` USD value.

    The current implementation reads from ``runs.total_nanodollars`` — which the
    aggregator updates lazily — so the value is at most one poll
    interval (500 ms by default) behind. Reports that demand
    strict consistency should call ``inkfoot rebuild-aggregates``
    first.
    """
    from decimal import Decimal  # noqa: PLC0415

    run_id = current_run_id()
    if run_id is None:
        raise NoActiveRun(
            "inkfoot.report_cost() called outside an active run."
        )
    storage = _resolve_storage()
    row = storage.get_run(run_id) if hasattr(storage, "get_run") else None
    if row is None:
        return Decimal("0")
    from inkfoot.money import nd_to_usd  # noqa: PLC0415

    return nd_to_usd(int(row.get("total_nanodollars") or 0))


# ----------------------------------------------------------------------
# agent_run — context manager / decorator / manual .start/.end
# ----------------------------------------------------------------------


class _RunHandle:
    """Holds the per-call state for one ``agent_run`` invocation:
    the run id, the ContextVar token (for clean restoration of the
    prior current-run), and a "started" flag so a double ``.start()``
    is a no-op rather than a crash.
    """

    def __init__(self, task: Optional[str], metadata: Optional[dict[str, Any]]):
        self._task = task
        self._metadata = metadata or {}
        self._run_id: Optional[str] = None
        self._context_token = None
        self._ended = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "_RunHandle":
        if self._run_id is not None:
            return self  # idempotent
        storage = _resolve_storage()
        run_id = f"run-{ULID()}"
        parent = current_run_id()  # capture before we mutate

        storage.start_run(
            run_id=run_id,
            task=self._task,
            agent_kind=self._metadata.get("agent_kind"),
            started_at=_now_ms(),
            parent_run_id=parent,
            metadata_json=(
                json.dumps(self._metadata, default=str) if self._metadata else None
            ),
        )
        self._run_id = run_id
        self._context_token = _set_current_run(run_id)
        from inkfoot.contracts import runtime as _contract_runtime

        _contract_runtime.on_run_start(run_id, self._task, self._metadata)
        _emit_event(
            run_id=run_id,
            kind="run_start",
            payload={
                "task": self._task,
                "parent_run_id": parent,
                "metadata": self._metadata,
            },
            storage=storage,
        )
        return self

    def end(
        self,
        status: str = "complete",
        error_message: Optional[str] = None,
    ) -> None:
        if self._ended:
            return
        if self._run_id is None:
            # Never started; nothing to do.
            self._ended = True
            return
        if status not in {"complete", "error"}:
            raise ValueError(
                f"_RunHandle.end: status must be 'complete' or 'error', "
                f"got {status!r}"
            )
        storage = _resolve_storage()
        _emit_event(
            run_id=self._run_id,
            kind="run_end",
            payload={"status": status, "error_message": error_message},
            storage=storage,
        )
        storage.end_run(
            run_id=self._run_id, ended_at=_now_ms(), status=status
        )
        if self._context_token is not None:
            _reset_current_run(self._context_token)
            self._context_token = None

        # Release per-run in-memory state so long-lived processes
        # don't accumulate one dict entry per run. Abandonment
        # cleanup does the same via _release_run_state below.
        _release_run_state(self._run_id)
        from inkfoot.contracts import runtime as _contract_runtime

        _contract_runtime.on_run_end(self._run_id)
        self._ended = True

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "_RunHandle":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.end(status="complete")
        else:
            self.end(
                status="error",
                error_message=f"{exc_type.__name__}: {exc}"[:1024],
            )

    # ------------------------------------------------------------------
    # Async context-manager protocol (PEP 492)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "_RunHandle":
        return self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def id(self) -> Optional[str]:
        return self._run_id


class _AgentRunFactory:
    """Returned by :func:`agent_run`. Acts as a context manager (when
    used in ``with``), a decorator (when used as ``@agent_run(...)``),
    or a handle factory (when used as ``agent_run(...).start()``)."""

    def __init__(
        self, task: Optional[str], metadata: Optional[dict[str, Any]]
    ):
        self._task = task
        self._metadata = metadata
        # Each context-manager use produces its own _RunHandle so a
        # ``with agent_run(task=t) as r1:`` followed by another with-
        # block on the same factory doesn't share state.
        self._handle: Optional[_RunHandle] = None

    def _make_handle(self) -> _RunHandle:
        return _RunHandle(self._task, self._metadata)

    def start(self) -> _RunHandle:
        return self._make_handle().start()

    # Context manager — instantiate a fresh handle per ``with``.
    def __enter__(self) -> _RunHandle:
        self._handle = self._make_handle()
        return self._handle.__enter__()

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._handle is not None
        self._handle.__exit__(exc_type, exc, tb)
        self._handle = None

    async def __aenter__(self) -> _RunHandle:
        self._handle = self._make_handle()
        return await self._handle.__aenter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._handle is not None
        await self._handle.__aexit__(exc_type, exc, tb)
        self._handle = None

    # Decorator — wrap ``fn`` so each call gets its own run.
    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        import asyncio  # noqa: PLC0415

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                handle = self._make_handle()
                async with handle:
                    return await fn(*args, **kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            handle = self._make_handle()
            with handle:
                return fn(*args, **kwargs)

        return sync_wrapper


def agent_run(
    *,
    task: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> _AgentRunFactory:
    """Decorator + context-manager + handle factory for one agent
    run.

    Usage::

        # Decorator
        @inkfoot.agent_run(task="customer-support-triage")
        def handle_ticket(ticket_id): ...

        # Context manager
        with inkfoot.agent_run(task="..."):
            ...

        # Manual
        run = inkfoot.agent_run(task="...").start()
        try: ...
        finally: run.end()
    """
    return _AgentRunFactory(task=task, metadata=metadata)


# ----------------------------------------------------------------------
# Per-run state cleanup
# ----------------------------------------------------------------------


def _release_run_state(run_id: str) -> None:
    """Release every per-run in-memory state slot for ``run_id``.

    The shim's per-run sequence counter (``inkfoot.shims._emit``)
    and the in-memory run-state map (``inkfoot._run_context``) both
    grow at one entry per run. Without explicit release at run-end
    they leak indefinitely in long-lived processes (Sleuth +
    internal tooling). This helper is the single cleanup point —
    called from :meth:`_RunHandle.end` for clean exits and from
    :func:`_mark_abandoned_runs` for crashes.

    Idempotent: calling on an already-released or unknown ``run_id``
    is a no-op.
    """
    # Late imports to avoid circular deps + match the style of
    # other lifecycle plumbing in this module.
    from inkfoot._run_context import _drop_run_state  # noqa: PLC0415
    from inkfoot.shims._emit import _drop_sequence_counter  # noqa: PLC0415

    try:
        _drop_sequence_counter(run_id)
    except Exception:  # pragma: no cover — defensive
        _LOG.warning(
            "_drop_sequence_counter failed for %s", run_id, exc_info=True
        )
    try:
        _drop_run_state(run_id)
    except Exception:  # pragma: no cover
        _LOG.warning(
            "_drop_run_state failed for %s", run_id, exc_info=True
        )


# ----------------------------------------------------------------------
# Abandonment detection (called from the atexit shutdown hook)
# ----------------------------------------------------------------------


def _mark_abandoned_runs() -> None:
    """Find every ``status='running'`` row at shutdown and flip it
    to ``status='error'`` with the error_message ``'abandoned'``.

    Called from ``inkfoot._instrument.shutdown`` so a process that
    exits between ``start_run`` and ``end_run`` leaves a clean row
    rather than a perpetual "running" zombie. Also releases the
    abandoned run's in-memory state so it doesn't outlive the row."""
    from inkfoot._instrument import _STORAGE  # noqa: PLC0415

    storage = _STORAGE
    if storage is None:
        return
    # getattr-guard: third-party Storage implementations written
    # against the older, narrower Protocol may not have this method;
    # for them abandonment cleanup is silently skipped rather than
    # crashing the atexit hook.
    finder = getattr(storage, "find_runs_with_status", None)
    if finder is None:
        return
    try:
        abandoned = finder("running")
    except Exception:  # pragma: no cover — defensive
        return
    for run_id in abandoned:
        try:
            _emit_event(
                run_id=run_id,
                kind="run_end",
                payload={"status": "error", "error_message": "abandoned"},
                storage=storage,
            )
            storage.end_run(
                run_id=run_id, ended_at=_now_ms(), status="error"
            )
        except Exception:  # pragma: no cover
            _LOG.warning(
                "abandoned-run cleanup failed for %s", run_id, exc_info=True
            )
        finally:
            # Release the abandoned run's in-memory state too —
            # by definition the user's __exit__ won't run.
            _release_run_state(run_id)
