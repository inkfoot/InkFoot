"""Storage layer Protocol + default SQLite implementation.

The Protocol is the contract every storage backend honours (SQLite in
Phase 0, Postgres in Phase 2). The contract is *narrow* — just the
methods the shim hot path and aggregator need.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from inkfoot.storage.sqlite import SQLiteStorage

__all__ = ["Storage", "SQLiteStorage", "RunRow", "DirtyRun"]


# Type aliases used by the Protocol surface. These are loose by design;
# the SQLite implementation tightens them.
RunRow = dict[str, Any]
DirtyRun = dict[str, Any]


@runtime_checkable
class Storage(Protocol):
    """Phase 0 storage Protocol (phase-0-classify §5.5, §5.6).

    The five core methods listed in the E1-S3 spec are
    ``connect``, ``insert_event``, ``mark_dirty``, ``read_dirty``,
    and ``update_aggregates``. We also expose ``start_run`` and
    ``end_run`` because ADR-0-1's two-tier write semantics require
    a synchronous status write; without them the protocol can't
    honour the architecture's fail-fast guarantee.
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
    ) -> None:
        """Append one event row + flip the parent run's
        ``aggregates_dirty`` flag in a single transaction (§5.6).
        Returns when the row is durable in WAL."""

    def mark_dirty(self, run_id: str) -> None:
        """Set ``aggregates_dirty=1`` on the named run. Used by
        ``inkfoot rebuild-aggregates`` to force a re-projection."""

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        """Return up to ``limit`` dirty run IDs for the aggregator
        to drain."""

    def update_aggregates(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> bool:
        """Update the projection columns for one run and clear its
        dirty flag, *iff* it's still dirty. Returns ``True`` if the
        row was updated, ``False`` if it was already clean (lost-
        update guard per §5.6)."""

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        """Stream a run's events in sequence order — what the
        aggregator projects from."""
