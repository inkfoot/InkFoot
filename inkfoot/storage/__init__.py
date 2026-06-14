"""Storage layer Protocol + lazy default-backend factory.

The Protocol is the contract every storage backend honours (SQLite
and Postgres). The contract is *narrow* — just the methods the shim
hot path and aggregator need.

The concrete backends are intentionally **not** imported at module
load: the Protocol module shouldn't pull in backend-specific symbols
when a test only wants the type. The lazy ``__getattr__`` below keeps
``from inkfoot.storage import SQLiteStorage`` (and
``PostgresStorage``) working for callers that do need them, while
decoupling import cost.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable

__all__ = [
    "Storage",
    "SQLiteStorage",
    "PostgresStorage",
    "default_storage",
    "RunRow",
    "DirtyRun",
    "PROJECTION_COLUMNS",
]


# Type aliases used by the Protocol surface. These are loose by
# design; the SQLite implementation tightens them.
RunRow = dict[str, Any]
DirtyRun = dict[str, Any]


# Subset of ``runs.*`` columns the projection layer is allowed to
# set. Shared by every backend's ``write_totals`` /
# ``update_aggregates`` so the allow-list can't drift between them.
PROJECTION_COLUMNS = frozenset(
    {
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_cache_creation_tokens",
        "total_nanodollars",
        "outcome",
        "quality_score",
    }
)


@runtime_checkable
class Storage(Protocol):
    """Storage Protocol .

    The methods listed in the storage protocol are ``connect``,
    ``insert_event``, ``mark_dirty``, ``read_dirty``, and
    ``update_aggregates``. We also expose ``start_run`` and
    ``end_run`` because the two-tier write contract requires a
    synchronous status write, and ``claim_clean`` / ``write_totals``
    because the never-lost-update guarantee requires the projection
    flow to read the event log *between* the claim and the write
    (see the SQLite implementation for the full explanation).
    """

    def connect(self) -> None:
        """Initialise the backend. Idempotent — safe to call twice."""

    def close(self) -> None:
        """Tear down the backend. Idempotent."""

    def start_run(
        self,
        *,
        run_id: str,
        task: Optional[str],
        agent_kind: Optional[str],
        started_at: int,
        parent_run_id: Optional[str] = None,
        run_kind: str = "root",
        metadata_json: Optional[str] = None,
    ) -> None:
        """Synchronous status write. Fails fast on storage
        unavailability so the agent never enters an incomplete
        instrumented state."""

    def end_run(
        self,
        *,
        run_id: str,
        ended_at: int,
        status: str,
    ) -> None:
        """Synchronous status update at run completion."""

    def insert_event(
        self,
        *,
        event_id: str,
        run_id: str,
        kind: str,
        occurred_at: int,
        sequence: int,
        payload_json: Optional[str] = None,
        capture_mode: str = "metadata",
        request_json: Optional[str] = None,
        response_json: Optional[str] = None,
        tool_result_json: Optional[str] = None,
        content_redacted: bool = False,
    ) -> None:
        """Append one event row + flip the parent run's
        ``aggregates_dirty`` flag in a single transaction.
        Returns when the row is durable in WAL.

        Replay-mode content kwargs (replay-mode storage contract): when
        ``capture_mode='replay'`` *and* any of ``request_json`` /
        ``response_json`` / ``tool_result_json`` is non-None, the
        implementation writes an ``event_contents`` row in the same
        transaction. ``capture_mode='metadata'`` always suppresses
        the sibling row. Backends that don't support replay should
        accept these kwargs (so the shim call site doesn't have to
        branch) and ignore them — a future Postgres backend may
        defer the replay implementation to future Cloud code.

        Backends that persist replay content may *optionally* expose a
        ``set_redaction_hook(hook)`` method; when present,
        ``inkfoot.instrument()`` installs the redaction hook there so
        sensitive bytes are masked before the content row is written.
        ``content_redacted`` records whether the hook changed anything.
        """

    def mark_dirty(self, run_id: str) -> None:
        """Set ``aggregates_dirty=1`` on the named run. Used by
        ``inkfoot rebuild-aggregates`` to force a re-projection."""

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        """Return up to ``limit`` dirty run IDs for the aggregator
        to drain."""

    def claim_clean(self, run_id: str) -> bool:
        """Atomic CAS that clears ``aggregates_dirty`` iff it was 1.
        Returns ``True`` when the caller successfully claimed the
        row's projection responsibility."""

    def write_totals(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> None:
        """Unconditional update of the projection columns. Does NOT
        touch ``aggregates_dirty``; that's :meth:`claim_clean`'s job
        and any concurrent :meth:`insert_event`'s natural
        consequence."""

    def update_aggregates(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> bool:
        """Convenience: composite ``claim_clean`` + ``write_totals``.
        Returns ``True`` if the row was dirty and the totals were
        written; ``False`` otherwise."""

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        """Stream a run's events in sequence order — what the
        aggregator projects from."""

    def find_runs_with_status(self, status: str) -> list[str]:
        """Return the IDs of every run currently in ``status``. The
        shutdown hook uses this to flip abandoned ``'running'`` rows
        to ``'error'`` without reaching into backend internals."""


def default_storage(path: Optional[Any] = None) -> "SQLiteStorage":
    """Return the default backend (:class:`SQLiteStorage`).

    Imported lazily so plain ``import inkfoot.storage`` doesn't pay
    the cost of pulling in the SQLite implementation module.
    """
    from inkfoot.storage.sqlite import SQLiteStorage as _SQLiteStorage

    return _SQLiteStorage(path=path)


def __getattr__(name: str) -> Any:
    """Lazy attribute access — re-exports the concrete backends
    without paying the import cost at module-load time."""
    if name == "SQLiteStorage":
        from inkfoot.storage.sqlite import SQLiteStorage as _SQLiteStorage

        return _SQLiteStorage
    if name == "PostgresStorage":
        from inkfoot.storage.postgres import (
            PostgresStorage as _PostgresStorage,
        )

        return _PostgresStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
