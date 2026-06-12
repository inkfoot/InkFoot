"""Tag-based group-by tests (``--group-by tag.<key>``).

Two layers:

* :func:`inkfoot.reports.tag_groupby.tag_buckets` unit tests against
  a small seeded SQLite database — value stringification,
  last-write-wins, unknown-bucket rules, window + task filters.
* An end-to-end ``_render_aggregate`` pass over a synthetic
  1000-run fixture with a known tag distribution, so the rendered
  bucket counts are hand-checkable.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from inkfoot.reports.tag_groupby import UNKNOWN_BUCKET, tag_buckets
from inkfoot.storage.sqlite import SQLiteStorage

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


def _storage(tmp_path) -> SQLiteStorage:
    s = SQLiteStorage(path=tmp_path / "runs.db")
    s.connect()
    return s


def _bulk_seed(storage, runs, tag_events) -> None:
    """Insert run rows + user_tag events in one transaction —
    seeding 1000 runs through ``start_run`` would pay one
    BEGIN IMMEDIATE/COMMIT round-trip per row."""
    conn = storage._conn()
    conn.execute("BEGIN")
    conn.executemany(
        """
        INSERT INTO runs (
            id, task, agent_kind, run_kind, started_at, ended_at,
            status, outcome, total_nanodollars
        ) VALUES (?, ?, 'agent', 'root', ?, ?, 'complete', ?, ?)
        """,
        runs,
    )
    conn.executemany(
        """
        INSERT INTO events (
            id, run_id, kind, occurred_at, payload_json, sequence,
            capture_mode
        ) VALUES (?, ?, 'user_tag', ?, ?, ?, 'metadata')
        """,
        tag_events,
    )
    conn.execute("COMMIT")


def _run_row(run_id, *, task="t", started_at=None, outcome="success", total=1_000_000):
    started = NOW_MS - 1000 if started_at is None else started_at
    return (run_id, task, started, NOW_MS, outcome, total)


def _tag_event(event_id, run_id, key, value, *, sequence=1):
    return (
        event_id,
        run_id,
        NOW_MS,
        json.dumps({"key": key, "value": value}),
        sequence,
    )


# ----------------------------------------------------------------------
# tag_buckets
# ----------------------------------------------------------------------


def test_maps_runs_to_stringified_tag_values(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1"), _run_row("r2"), _run_row("r3")],
            [
                _tag_event("e1", "r1", "tier", "gold"),
                _tag_event("e2", "r2", "tier", 5),
                _tag_event("e3", "r3", "tier", True),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {"r1": "gold", "r2": "5", "r3": "True"}


def test_last_tag_write_wins(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1")],
            [
                _tag_event("e1", "r1", "tier", "free", sequence=1),
                _tag_event("e2", "r1", "tier", "pro", sequence=2),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {"r1": "pro"}


@pytest.mark.parametrize("late_value", [None, ""])
def test_later_unusable_value_overwrites_earlier_real_value(
    tmp_path, late_value
) -> None:
    """Last-write-wins applies to unusable values too: a later
    ``None`` / empty-string write demotes the run to the unknown
    bucket rather than reviving the earlier real value."""
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1")],
            [
                _tag_event("e1", "r1", "tier", "free", sequence=1),
                _tag_event("e2", "r1", "tier", late_value, sequence=2),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {"r1": UNKNOWN_BUCKET}


def test_null_and_empty_tag_values_bucket_as_unknown(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1"), _run_row("r2")],
            [
                _tag_event("e1", "r1", "tier", None),
                _tag_event("e2", "r2", "tier", ""),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {"r1": UNKNOWN_BUCKET, "r2": UNKNOWN_BUCKET}


def test_other_tag_keys_are_ignored(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1")],
            [_tag_event("e1", "r1", "region", "eu")],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {}


def test_window_cutoff_excludes_older_runs(tmp_path) -> None:
    since = NOW_MS - DAY_MS
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [
                _run_row("r-old", started_at=since - 5000),
                _run_row("r-new", started_at=since + 5000),
            ],
            [
                _tag_event("e1", "r-old", "tier", "gold"),
                _tag_event("e2", "r-new", "tier", "gold"),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=since)
    finally:
        s.close()
    assert buckets == {"r-new": "gold"}


def test_task_filter_limits_the_scan(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1", task="triage"), _run_row("r2", task="search")],
            [
                _tag_event("e1", "r1", "tier", "gold"),
                _tag_event("e2", "r2", "tier", "gold"),
            ],
        )
        buckets = tag_buckets(
            s._conn(),
            key="tier",
            since_ms=NOW_MS - DAY_MS,
            task_filter="triage",
        )
    finally:
        s.close()
    assert buckets == {"r1": "gold"}


def test_malformed_tag_payloads_are_skipped(tmp_path) -> None:
    s = _storage(tmp_path)
    try:
        _bulk_seed(
            s,
            [_run_row("r1"), _run_row("r2")],
            [
                ("e1", "r1", NOW_MS, "{not json", 1),
                ("e2", "r2", NOW_MS, None, 1),
            ],
        )
        buckets = tag_buckets(s._conn(), key="tier", since_ms=NOW_MS - DAY_MS)
    finally:
        s.close()
    assert buckets == {}


# ----------------------------------------------------------------------
# End-to-end: 1000 synthetic runs through the aggregate view
# ----------------------------------------------------------------------


def _thousand_run_fixture():
    """1000 runs, $0.001 each, with a deterministic tag mix:

    * 400 tagged ``customer_tier=free``  (i % 5 in {0, 3})
    * 200 tagged ``customer_tier=pro``   (i % 5 == 1)
    * 200 tagged ``customer_tier=enterprise`` (i % 5 == 2)
    * 200 untagged (i % 5 == 4) — of which half carry no outcome
      and must land in ``uninstrumented``, not ``unknown``.

    Every ``pro`` run also carries an unrelated ``region`` tag to
    prove other keys never leak into the bucketing.
    """
    runs, events = [], []
    for i in range(1000):
        run_id = f"r{i}"
        slot = i % 5
        outcome = "success"
        if slot == 4 and i % 10 == 9:
            outcome = None
        runs.append(_run_row(run_id, outcome=outcome))
        tier = {0: "free", 1: "pro", 2: "enterprise", 3: "free"}.get(slot)
        if tier is not None:
            events.append(_tag_event(f"e{i}", run_id, "customer_tier", tier))
        if slot == 1:
            events.append(
                _tag_event(f"x{i}", run_id, "region", "eu", sequence=2)
            )
    return runs, events


def test_thousand_runs_group_into_hand_checked_tag_buckets(tmp_path) -> None:
    from inkfoot.cli.report import _render_aggregate

    s = _storage(tmp_path)
    try:
        runs, events = _thousand_run_fixture()
        _bulk_seed(s, runs, events)
        args = SimpleNamespace(
            last="1d",
            task=None,
            group_by="tag.customer_tier",
            no_smells=True,
        )
        out = _render_aggregate(s, args)
    finally:
        s.close()

    def row(bucket):
        lines = [
            ln for ln in out.splitlines() if ln.split()[:1] == [bucket]
        ]
        assert len(lines) == 1, f"expected one {bucket!r} row in:\n{out}"
        return lines[0].split()

    # bucket, runs — spend sort: free (400M), enterprise/pro (200M
    # tie, name order), unknown (100M), uninstrumented pinned last.
    assert row("free")[1] == "400"
    assert row("pro")[1] == "200"
    assert row("enterprise")[1] == "200"
    assert row("unknown")[1] == "100"
    assert row("uninstrumented")[1] == "100"

    order = [
        ln.split()[0]
        for ln in out.splitlines()
        if ln.split()[:1]
        and ln.split()[0]
        in ("free", "pro", "enterprise", "unknown", "uninstrumented")
    ]
    assert order == ["free", "enterprise", "pro", "unknown", "uninstrumented"]

    # All-success buckets: cost/success == avg == $0.0010.
    assert row("free")[2] == "$0.0010"
    # The unknown bucket is instrumented — its outcome math stays
    # live; only the uninstrumented row dashes out.
    assert row("unknown")[2] == "$0.0010"
    assert row("uninstrumented")[2] == "—"
