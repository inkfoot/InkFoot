"""Tests for the inline smells rendering in ``inkfoot report --run``.

The renderer already accepts an explicit ``smells`` argument; what
these tests pin is the CLI dispatcher's behaviour:

* By default, single-run mode runs the smell engine and surfaces
  detections in the output.
* ``--no-smells`` skips the engine and renders a clean attribution
  view.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest
from ulid import ULID

from inkfoot.cli import report as report_cli
from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.storage.sqlite import SQLiteStorage


def _seeded_storage(tmp_path) -> tuple[SQLiteStorage, str]:
    """A SQLiteStorage with one run that's guaranteed to trip the
    `unstable-prompt-prefix` smell.

    The smell fires when more than 10% of the run's combined system
    block is *dynamic*. We emit two calls whose system block is
    half-static, half-dynamic so the detector trips on the first
    eligible run."""
    storage = SQLiteStorage(path=tmp_path / "report-smells.db")
    storage.connect()
    run_id = f"run-{ULID()}"
    storage.start_run(
        run_id=run_id,
        task="unit-test",
        agent_kind="test",
        started_at=1_700_000_000_000,
    )

    for idx in range(1, 3):
        call = NeutralCall(
            provider="anthropic",
            model="claude-haiku-4-5",
            started_at=1_700_000_000_000 + idx * 1000,
            ended_at=1_700_000_000_500 + idx * 1000,
            ledger=CausalTokenLedger(
                system_static_tokens=100,
                # Half-dynamic / half-static system block is well
                # above the 10% trip threshold.
                system_dynamic_tokens=100,
                user_input_tokens=10,
                output_tokens=5,
            ),
            estimated_nanodollars=1_000_000,
            sequence=idx,
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

    storage.end_run(run_id=run_id, ended_at=1_700_000_010_000, status="complete")
    return storage, run_id


def _args(run_id: str, *, no_smells: bool = False, db: Any = None) -> SimpleNamespace:
    """Build the minimal args object the report CLI consumes."""
    return SimpleNamespace(
        db=str(db) if db else None,
        run=run_id,
        last=None,
        task=None,
        group_by="task",
        show_zero=False,
        no_smells=no_smells,
    )


def test_single_run_report_includes_smells_stanza_by_default(tmp_path, capsys):
    storage, run_id = _seeded_storage(tmp_path)
    try:
        rc = report_cli.run(_args(run_id, db=storage._path))
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Smells detected" in out


def test_no_smells_flag_hides_smells_stanza(tmp_path, capsys):
    storage, run_id = _seeded_storage(tmp_path)
    try:
        rc = report_cli.run(
            _args(run_id, db=storage._path, no_smells=True)
        )
    finally:
        storage.close()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Smells detected" not in out
    # The attribution chart still renders — `--no-smells` only
    # suppresses the smells block.
    assert "Causal attribution:" in out
