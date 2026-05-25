"""Run-level dataclasses — the persistence shape and the in-memory
sidecar.

Two distinct objects live here:

* :class:`Run` mirrors the SQLite ``runs`` table 1:1 — every column,
  same names, same types (modulo the integer-vs-bool translation
  Python does naturally). It's what the storage layer reads and
  writes, and what the report CLI deserialises.

* :class:`InMemoryRunState` is the *process-local* sidecar for a
  live run: the longest-stable-prefix string for attribution, the
  rolling list of recent calls, the retry-count map. Lives only in
  the instrumented process's memory and is intentionally *not*
  persisted. It vanishes when the process exits — that's fine,
  because the event log is the source of truth and aggregation
  reconstructs the visible run state.

The split exists because mixing the two would make either
persistence or attribution awkward: a frozen :class:`Run` is fine to
hand around, but mutation on the stable-prefix string needs to
happen on every call, so :class:`InMemoryRunState` is explicitly
mutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# The exact set of columns on the SQLite ``runs`` table (per the v1
# migration in inkfoot/storage/migrations.py). Used by the reflection
# test that pins :class:`Run` to the schema; kept here so an
# implementer who adds a column without updating the dataclass is
# caught by the test rather than at runtime.
RUN_TABLE_COLUMNS: tuple[str, ...] = (
    "id",
    "task",
    "agent_kind",
    "parent_run_id",
    "run_kind",
    "divergence_flag",
    "started_at",
    "ended_at",
    "status",
    "outcome",
    "quality_score",
    "total_input_tokens",
    "total_output_tokens",
    "total_cache_read_tokens",
    "total_cache_creation_tokens",
    "total_nanodollars",
    "aggregates_dirty",
    "metadata_json",
)


@dataclass(frozen=True, slots=True)
class Run:
    """Persistence-shape representation of one agent run.

    Every field matches a column on the SQLite ``runs`` table. The
    aggregator updates the ``total_*`` projections from the event
    log; everything else is a primary fact written at run lifecycle
    boundaries.

    Note ``metadata_json`` is stored as a JSON-encoded string on the
    table (SQLite text). Callers that want the parsed dict should
    deserialise themselves — keeping the dataclass faithful to the
    storage shape avoids per-read JSON parsing in hot paths like the
    aggregator sweep.
    """

    id: str
    task: Optional[str] = None
    agent_kind: Optional[str] = None
    parent_run_id: Optional[str] = None
    run_kind: str = "root"
    divergence_flag: Optional[int] = None
    started_at: int = 0
    ended_at: Optional[int] = None
    status: str = "running"
    outcome: Optional[str] = None
    quality_score: Optional[float] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_nanodollars: int = 0
    aggregates_dirty: int = 0
    metadata_json: Optional[str] = None


@dataclass
class InMemoryRunState:
    """Process-local sidecar for one live run. **Not persisted.**

    Holds the things attribution recipes need but the SQLite schema
    doesn't (and shouldn't) carry:

    * ``stable_system_prefix`` — the running longest-common prefix
      of every system block seen so far. Updates monotonically
      (shortens only). Phase 0's smell engine reads this to flag
      ``UnstablePromptPrefix`` violations.
    * ``recent_calls`` — append-only list of :class:`NeutralCall`
      payloads for the live run, cached so smells like
      ``RunawayRetryLoop`` can pattern-match without re-querying
      the event log.
    * ``retry_counts`` — ``{cause_signature: count}`` rolling map
      used by the retry-overhead attribution.

    There's no save/load helper on this class on purpose: the
    instrumented process owns its lifetime, full stop. A test that
    finds a storage helper here should fail.
    """

    stable_system_prefix: str = ""
    recent_calls: list = field(default_factory=list)
    retry_counts: dict[str, int] = field(default_factory=dict)
