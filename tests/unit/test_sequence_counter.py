"""Tests for the per-run sequence counter — Finding #1 in review.

The pre-fix dict-check-then-set pattern was racy: two threads could
each create a separate ``itertools.count`` for the same run_id and
return identical sequences. The fix moved the entire read-or-init +
increment under a lock; this test pins the fix against 8-thread
contention.
"""

from __future__ import annotations

import threading

import pytest

from inkfoot.shims._emit import (
    _drop_sequence_counter,
    _next_sequence,
    _sequence_counters,
)


@pytest.fixture(autouse=True)
def reset_counters() -> None:
    """Each test starts with the counter dict empty."""
    _sequence_counters.clear()
    yield
    _sequence_counters.clear()


def test_first_call_returns_one() -> None:
    assert _next_sequence("r1") == 1


def test_subsequent_calls_in_one_thread_are_monotonic() -> None:
    values = [_next_sequence("r1") for _ in range(5)]
    assert values == [1, 2, 3, 4, 5]


def test_distinct_runs_have_independent_counters() -> None:
    assert _next_sequence("a") == 1
    assert _next_sequence("b") == 1
    assert _next_sequence("a") == 2
    assert _next_sequence("b") == 2


def test_concurrent_increments_have_no_duplicates() -> None:
    """The headline race the reviewer flagged: 8 threads × 250
    increments under one run_id. The set of returned values must
    have size 2000 (no duplicates) and cover [1, 2000] exactly.
    """
    n_threads = 8
    per_thread = 250
    total = n_threads * per_thread
    out: list[int] = []
    out_lock = threading.Lock()
    start = threading.Event()

    def worker() -> None:
        start.wait()
        local: list[int] = []
        for _ in range(per_thread):
            local.append(_next_sequence("contended-run"))
        with out_lock:
            out.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert len(out) == total
    # No duplicates.
    assert len(set(out)) == total
    # Covers exactly [1, total].
    assert min(out) == 1
    assert max(out) == total


def test_drop_sequence_counter_clears_entry() -> None:
    _next_sequence("doomed")
    _next_sequence("doomed")
    _drop_sequence_counter("doomed")
    # A fresh increment starts back at 1.
    assert _next_sequence("doomed") == 1


def test_drop_sequence_counter_is_idempotent_on_unknown_id() -> None:
    # No exception even when the run was never seen.
    _drop_sequence_counter("never-existed")
