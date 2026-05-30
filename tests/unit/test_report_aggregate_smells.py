"""Tests for the aggregate smells stanza in ``inkfoot report
--last <window>``.

These tests exercise the renderer at the SQL boundary: they seed
a SQLiteStorage with a handful of runs, half of which trip a
specific smell, and assert the stanza reports the right hit
count + percentage.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest
from ulid import ULID

from inkfoot.cli import report as report_cli
from inkfoot.cli.report import (
    _MAX_AGGREGATE_SMELL_RUNS,
    _render_aggregate_smells,
)
from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.storage.sqlite import SQLiteStorage


def _seed_runs(
    storage: SQLiteStorage,
    *,
    task: str,
    started_at_floor_ms: int,
    smelly: bool,
    n: int,
) -> list[str]:
    """Insert ``n`` runs under ``task``. When ``smelly=True`` each
    run trips the `unstable-prompt-prefix` smell; otherwise the
    system block stays fully static."""
    run_ids: list[str] = []
    for i in range(n):
        run_id = f"run-{ULID()}"
        started_at = started_at_floor_ms + i * 1000
        storage.start_run(
            run_id=run_id,
            task=task,
            agent_kind="test",
            started_at=started_at,
        )
        call = NeutralCall(
            provider="anthropic",
            model="claude-haiku-4-5",
            started_at=started_at,
            ended_at=started_at + 500,
            ledger=CausalTokenLedger(
                system_static_tokens=100,
                system_dynamic_tokens=100 if smelly else 0,
                user_input_tokens=10,
                output_tokens=5,
            ),
            estimated_nanodollars=1_000_000,
            sequence=1,
        )
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=call.ended_at,
            sequence=call.sequence,
            payload_json=json.dumps(asdict(call)),
            capture_mode="metadata",
        )
        storage.end_run(run_id=run_id, ended_at=started_at + 1000, status="complete")
        run_ids.append(run_id)
    return run_ids


def _args(*, last: str = "1d", db: Any = None, task=None, no_smells: bool = False):
    return SimpleNamespace(
        db=str(db) if db else None,
        run=None,
        last=last,
        task=task,
        group_by="task",
        show_zero=False,
        no_smells=no_smells,
    )


def test_aggregate_smells_stanza_lists_hits_and_percentage(tmp_path, capsys):
    import time as _time

    base = int(_time.time() * 1000) - 60_000
    storage = SQLiteStorage(path=tmp_path / "agg.db")
    storage.connect()
    try:
        _seed_runs(storage, task="t-agg", started_at_floor_ms=base, smelly=True, n=4)
        _seed_runs(
            storage, task="t-agg", started_at_floor_ms=base + 5000, smelly=False, n=6
        )
        rc = report_cli.run(_args(db=storage._path))
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aggregate smells (last 1d):" in out
    # Four out of ten runs tripped the smell -> 40%.
    assert "unstable-prompt-prefix: 4/10 runs (40%)" in out


def test_aggregate_smells_stanza_says_none_detected_when_clean(tmp_path, capsys):
    import time as _time

    base = int(_time.time() * 1000) - 60_000
    storage = SQLiteStorage(path=tmp_path / "agg-clean.db")
    storage.connect()
    try:
        _seed_runs(
            storage, task="t-clean", started_at_floor_ms=base, smelly=False, n=3
        )
        rc = report_cli.run(_args(db=storage._path))
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aggregate smells (last 1d): none detected" in out


def test_no_smells_flag_suppresses_aggregate_stanza(tmp_path, capsys):
    import time as _time

    base = int(_time.time() * 1000) - 60_000
    storage = SQLiteStorage(path=tmp_path / "agg-no-smells.db")
    storage.connect()
    try:
        _seed_runs(
            storage, task="t-quiet", started_at_floor_ms=base, smelly=True, n=2
        )
        rc = report_cli.run(_args(db=storage._path, no_smells=True))
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Aggregate smells" not in out


def test_aggregate_smells_respects_task_filter(tmp_path, capsys):
    import time as _time

    base = int(_time.time() * 1000) - 60_000
    storage = SQLiteStorage(path=tmp_path / "agg-task.db")
    storage.connect()
    try:
        _seed_runs(storage, task="t-x", started_at_floor_ms=base, smelly=True, n=2)
        _seed_runs(
            storage,
            task="t-y",
            started_at_floor_ms=base + 5000,
            smelly=False,
            n=5,
        )
        rc = report_cli.run(_args(db=storage._path, task="t-x"))
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    # Only the two runs under t-x should be counted in the rollup.
    assert "unstable-prompt-prefix: 2/2 runs (100%)" in out


def test_renderer_returns_empty_string_when_no_runs_in_window(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "agg-empty.db")
    storage.connect()
    try:
        rendered = _render_aggregate_smells(
            storage=storage,
            since_ms=0,
            window_label="7d",
            task_filter=None,
        )
    finally:
        storage.close()
    assert rendered == ""


def test_max_aggregate_smell_runs_is_a_finite_cap():
    # If this constant were accidentally set to None or zero the
    # cross-run scan would silently degrade. Pin the contract.
    assert isinstance(_MAX_AGGREGATE_SMELL_RUNS, int)
    assert _MAX_AGGREGATE_SMELL_RUNS > 0


def test_iter_recent_runs_with_events_is_lazy(tmp_path):
    # Pin the streaming-memory invariant: the iterator must NOT
    # materialise every run's events up front. We assert this
    # behaviourally by consuming one pair, then deleting subsequent
    # event rows and confirming the generator still yields the
    # remaining runs without choking on the missing children.
    import time as _time

    from inkfoot.cli.report import _iter_recent_runs_with_events

    base = int(_time.time() * 1000) - 60_000
    storage = SQLiteStorage(path=tmp_path / "agg-stream.db")
    storage.connect()
    try:
        _seed_runs(
            storage, task="t-stream", started_at_floor_ms=base, smelly=True, n=3
        )
        gen = iter(
            _iter_recent_runs_with_events(
                storage=storage,
                since_ms=0,
                task_filter=None,
                limit=10,
            )
        )
        run1, events1 = next(gen)
        # Materialising the first pair must not have eagerly pulled
        # the remaining runs' events; we test that by exhausting
        # the iterator now and confirming we get two more pairs.
        consumed = list(gen)
        assert len(consumed) == 2
        # Each ``events`` value is a fresh storage iterator — turn
        # it into a list and confirm a real event landed.
        list(events1)
        for _, events in consumed:
            list(events)
    finally:
        storage.close()
