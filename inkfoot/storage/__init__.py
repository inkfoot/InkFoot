"""Storage layer Protocol + lazy default-backend factory.

The Protocol is the contract every storage backend honours (SQLite in
Phase 0, Postgres in Phase 2). The contract is *narrow* — just the
methods the shim hot path and aggregator need.

``SQLiteStorage`` is intentionally **not** imported at module load:
the Protocol module shouldn't pull in SQLite-specific symbols when a
test only wants the type. The lazy ``__getattr__`` below keeps
``from inkfoot.storage import SQLiteStorage`` working for callers
that do need it, while decoupling import cost.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable

__all__ = ["Storage", "SQLiteStorage", "default_storage", "RunRow", "DirtyRun"]


# Type aliases used by the Protocol surface. These are loose by
# design; the SQLite implementation tightens them.
RunRow = dict[str, Any]
DirtyRun = dict[str, Any]


@runtime_checkable
class Storage(Protocol):
    """Phase 0 storage Protocol (phase-0-classify §5.5, §5.6).

    The methods listed in the E1-S3 spec are ``connect``,
    ``insert_event``, ``mark_dirty``, ``read_dirty``, and
    ``update_aggregates``. We also expose ``start_run`` and
    ``end_run`` because ADR-0-1's two-tier write semantics require a
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
        """Synchronous status write (ADR-0-1). Fails fast on storage
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
        ``aggregates_dirty`` flag in a single transaction (§5.6).
        Returns when the row is durable in WAL.

        Replay-mode content kwargs (ADR-0-9): when
        ``capture_mode='replay'`` *and* any of ``request_json`` /
        ``response_json`` / ``tool_result_json`` is non-None, the
        implementation writes an ``event_contents`` row in the same
        transaction. ``capture_mode='metadata'`` always suppresses
        the sibling row. Backends that don't support replay should
        accept these kwargs (so the shim call site doesn't have to
        branch) and ignore them — Phase 2's Postgres backend may
        defer the replay implementation to Phase 3.
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


def default_storage(path: Optional[Any] = None) -> "SQLiteStorage":
    """Return the default Phase 0 backend (:class:`SQLiteStorage`).

    Imported lazily so plain ``import inkfoot.storage`` doesn't pay
    the cost of pulling in the SQLite implementation module.
    """
    from inkfoot.storage.sqlite import SQLiteStorage as _SQLiteStorage

    return _SQLiteStorage(path=path)


def __getattr__(name: str) -> Any:
    """Lazy attribute access — re-exports :class:`SQLiteStorage`
    without paying the import cost at module-load time."""
    if name == "SQLiteStorage":
        from inkfoot.storage.sqlite import SQLiteStorage as _SQLiteStorage

        return _SQLiteStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
