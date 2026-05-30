"""Unit tests for ``inkfoot.cli.tail``.

These cover the pure pieces (line formatter, ``--since`` parser,
short-id, payload-field projections) and the loop core driven
against a real :class:`SQLiteStorage` so the SQL filter behaves
the way live tails will.
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

import pytest
from ulid import ULID

from inkfoot.cli import tail as tail_cli
from inkfoot.cli.tail import (
    TailArgError,
    _parse_since,
    _short_run_id,
    fetch_new_events,
    format_event_line,
    tail_loop,
)
from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# --since parser
# ----------------------------------------------------------------------


def test_parse_since_returns_none_for_falsy_input():
    assert _parse_since(None) is None
    assert _parse_since("") is None


def test_parse_since_handles_each_supported_unit():
    now_ms = int(time.time() * 1000)
    for value, expected_seconds in (
        ("30s", 30),
        ("10m", 600),
        ("2h", 7_200),
        ("7d", 604_800),
    ):
        floor = _parse_since(value)
        assert floor is not None
        # Allow a small slack for the wall-clock read inside the call.
        assert abs(floor - (now_ms - expected_seconds * 1000)) < 1_000


def test_parse_since_rejects_unknown_unit():
    with pytest.raises(TailArgError, match="invalid --since"):
        _parse_since("3w")


def test_parse_since_rejects_unparseable_input():
    with pytest.raises(TailArgError, match="invalid --since"):
        _parse_since("ten minutes")


# ----------------------------------------------------------------------
# Short run id formatting
# ----------------------------------------------------------------------


def test_short_run_id_keeps_last_eight_chars():
    # ``run-`` is stripped first so the cut never lands inside the
    # hyphen and operators can copy-paste the visible suffix back
    # into ``inkfoot report --run <id>``. Input strips to
    # "01HZX0ABCDEFGHJK" (16 chars); the last eight are "CDEFGHJK".
    assert _short_run_id("run-01HZX0ABCDEFGHJK") == "CDEFGHJK"


def test_short_run_id_strips_run_prefix_before_slicing():
    # Short ids with the prefix should not surface the prefix's
    # final hyphen in the rendered suffix.
    assert _short_run_id("run-01HZ") == "01HZ".ljust(8)


def test_short_run_id_pads_short_inputs():
    assert _short_run_id("xy") == "xy".ljust(8)


def test_short_run_id_handles_empty():
    out = _short_run_id("")
    assert len(out) == 8
    assert "?" in out


# ----------------------------------------------------------------------
# format_event_line — projections per event kind
# ----------------------------------------------------------------------


def _evt(
    kind: str,
    *,
    occurred_at: int = 1_700_000_000_500,
    run_id: str = "run-01H-ABCDEFGH",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "occurred_at": occurred_at,
        "run_id": run_id,
        "payload_json": json.dumps(payload or {}),
    }


def test_format_line_for_llm_call_includes_provider_and_tokens():
    line = format_event_line(
        _evt(
            "llm_call",
            payload={
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "system_static_tokens": 50,
                "user_input_tokens": 30,
                "output_tokens": 20,
                "estimated_nanodollars": 12_345,
            },
        )
    )
    assert "llm_call" in line
    assert "provider=anthropic" in line
    assert "model=claude-haiku-4-5" in line
    assert "input_tokens=80" in line
    assert "output_tokens=20" in line
    assert "cost_nd=12345" in line


def test_format_line_for_outcome_carries_outcome_and_quality():
    line = format_event_line(
        _evt(
            "outcome",
            payload={"outcome": "success", "quality_score": 0.94},
        )
    )
    assert "outcome=success" in line
    assert "quality=0.94" in line


def test_format_line_for_unknown_kind_falls_through_to_safe_prefix_projection():
    # The generic projection allow-lists names that start with a
    # small set of safe prefixes (id/name/label/status/kind/task/
    # count/stage/type). Of the payload below, only "label",
    # "status", and "task" pass — sorted -> [label, status, task] —
    # so all three should surface and unrelated keys should not.
    line = format_event_line(
        _evt(
            "mystery_kind",
            payload={
                "label": "X",
                "status": "ok",
                "task": "demo",
                "other": "shouldNotAppear",
            },
        )
    )
    assert "label=X" in line
    assert "status=ok" in line
    assert "task=demo" in line
    assert "other" not in line


def test_format_line_generic_fallthrough_denies_suspicious_keys():
    # Anything that doesn't start with a safe prefix is denied,
    # including names that would leak credentials. This guards
    # against a future event kind smuggling a secret into the
    # payload — keys like ``api_key`` / ``token`` / ``password``
    # never surface in the tail line.
    line = format_event_line(
        _evt(
            "future_kind",
            payload={
                "api_key": "sk-supersecret",
                "token": "ghp_supersecret",
                "password": "p@ssw0rd",
                "amount": 42,
            },
        )
    )
    assert "api_key" not in line
    assert "token" not in line
    assert "password" not in line
    assert "amount" not in line


def test_format_line_truncates_long_payload_values():
    payload = {"label": "x" * 500}
    line = format_event_line(_evt("checkpoint", payload=payload))
    # The trimmed value ends with an ellipsis sentinel.
    assert "…" in line
    assert "x" * 500 not in line


def test_format_line_handles_corrupt_payload_json():
    # When the row has malformed JSON the line should still render —
    # just without payload-field fragments.
    evt = {
        "kind": "llm_call",
        "occurred_at": 1_700_000_000_500,
        "run_id": "run-x",
        "payload_json": "{not json",
    }
    line = format_event_line(evt)
    assert "llm_call" in line
    assert "provider=" not in line  # we suppressed empty fields


# ----------------------------------------------------------------------
# Loop core driven against real SQLiteStorage
# ----------------------------------------------------------------------


def _seed_event(storage: SQLiteStorage, *, run_id: str, kind: str, sequence: int):
    storage.insert_event(
        event_id=str(ULID()),
        run_id=run_id,
        kind=kind,
        occurred_at=1_700_000_000_000 + sequence,
        sequence=sequence,
        payload_json=json.dumps({"sequence": sequence}),
        capture_mode="metadata",
    )


def test_fetch_new_events_picks_up_rows_above_cursor(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-cursor.db")
    storage.connect()
    try:
        storage.start_run(
            run_id="run-A", task="t-A", agent_kind="x", started_at=1
        )
        _seed_event(storage, run_id="run-A", kind="llm_call", sequence=1)
        rows, cursor1 = fetch_new_events(
            storage=storage, cursor=0, task_filter=None, limit=100
        )
        assert len(rows) == 1
        _seed_event(storage, run_id="run-A", kind="checkpoint", sequence=2)
        rows2, cursor2 = fetch_new_events(
            storage=storage, cursor=cursor1, task_filter=None, limit=100
        )
        assert [row["kind"] for row in rows2] == ["checkpoint"]
        assert cursor2 > cursor1
    finally:
        storage.close()


def test_fetch_new_events_honours_task_filter(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-task.db")
    storage.connect()
    try:
        storage.start_run(run_id="run-T", task="wanted", agent_kind="x", started_at=1)
        storage.start_run(run_id="run-U", task="other", agent_kind="x", started_at=2)
        _seed_event(storage, run_id="run-T", kind="llm_call", sequence=1)
        _seed_event(storage, run_id="run-U", kind="llm_call", sequence=2)
        rows, _ = fetch_new_events(
            storage=storage, cursor=0, task_filter="wanted", limit=100
        )
        assert [row["run_id"] for row in rows] == ["run-T"]
    finally:
        storage.close()


def test_tail_loop_with_since_none_skips_pre_existing_events(tmp_path):
    # ``--since`` is unset -> tail starts from "now". Pre-existing
    # events in the DB should NOT scroll past the terminal — that's
    # the contract the CLI help text promises.
    storage = SQLiteStorage(path=tmp_path / "tail-live-default.db")
    storage.connect()
    try:
        storage.start_run(
            run_id="run-pre", task="t", agent_kind="x", started_at=1
        )
        _seed_event(storage, run_id="run-pre", kind="llm_call", sequence=1)
        _seed_event(storage, run_id="run-pre", kind="outcome", sequence=2)
        buf = io.StringIO()
        emitted = tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=None,
            poll_interval_s=0.0,
            max_iterations=1,
            writer=buf,
            sleep=lambda _: None,
        )
        assert emitted == 0
        assert buf.getvalue() == ""
    finally:
        storage.close()


def test_tail_loop_with_since_none_picks_up_events_inserted_after_start(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-default-live.db")
    storage.connect()
    try:
        storage.start_run(
            run_id="run-pre", task="t", agent_kind="x", started_at=1
        )
        _seed_event(storage, run_id="run-pre", kind="llm_call", sequence=1)
        buf = io.StringIO()
        # The first iteration pins the live cursor and surfaces nothing;
        # the second iteration picks up the new event inserted in
        # between via the sleep hook.
        def _sleep(_):
            _seed_event(storage, run_id="run-pre", kind="checkpoint", sequence=2)

        tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=None,
            poll_interval_s=0.0,
            max_iterations=2,
            writer=buf,
            sleep=_sleep,
        )
        assert "checkpoint" in buf.getvalue()
        assert "llm_call" not in buf.getvalue()
    finally:
        storage.close()


def test_tail_loop_emits_lines_for_new_events(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-emit.db")
    storage.connect()
    try:
        storage.start_run(
            run_id="run-Z", task="emit", agent_kind="x", started_at=1
        )
        _seed_event(storage, run_id="run-Z", kind="llm_call", sequence=1)
        _seed_event(storage, run_id="run-Z", kind="outcome", sequence=2)
        buf = io.StringIO()
        emitted = tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=0,
            poll_interval_s=0.0,
            max_iterations=1,
            writer=buf,
            sleep=lambda _: None,
        )
        # ``since_ms=0`` puts the cursor at "the start of time" so
        # the first iteration backfills everything.
        assert emitted == 2
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        assert len(lines) == 2
        assert "llm_call" in lines[0]
        assert "outcome" in lines[1]
    finally:
        storage.close()


def test_tail_loop_skips_events_older_than_since_window(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-since.db")
    storage.connect()
    try:
        storage.start_run(
            run_id="run-S", task="t", agent_kind="x", started_at=1
        )
        # Insert an "old" event, then bump --since past it.
        _seed_event(storage, run_id="run-S", kind="llm_call", sequence=1)
        # Resolve cutoff after inserting the old one, then insert
        # a fresh event the loop should pick up.
        cutoff_ms = 1_700_000_000_100
        _seed_event(storage, run_id="run-S", kind="checkpoint", sequence=200)
        buf = io.StringIO()
        tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=cutoff_ms,
            poll_interval_s=0.0,
            max_iterations=1,
            writer=buf,
            sleep=lambda _: None,
        )
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        # The old llm_call (occurred_at=1_700_000_000_001) is below
        # the cutoff; only the checkpoint should surface.
        assert len(lines) == 1
        assert "checkpoint" in lines[0]
    finally:
        storage.close()


def test_tail_loop_max_iterations_stops_the_loop(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-iter.db")
    storage.connect()
    try:
        sleep_calls: list[float] = []
        emitted = tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=None,
            poll_interval_s=0.5,
            max_iterations=3,
            writer=io.StringIO(),
            sleep=lambda s: sleep_calls.append(s),
        )
        assert emitted == 0
        # ``max_iterations=3`` means three polls then exit — sleep
        # fires between polls but doesn't fire after the last poll
        # since the loop checks the iteration cap immediately after
        # the poll.
        assert sleep_calls == [0.5, 0.5]
    finally:
        storage.close()


def test_run_returns_two_when_poll_interval_is_invalid(tmp_path, capsys):
    from types import SimpleNamespace

    args = SimpleNamespace(
        db=str(tmp_path / "tail-bad.db"),
        task=None,
        since=None,
        poll_interval_ms=0,
        max_iterations=1,
    )
    rc = tail_cli.run(args)
    assert rc == 2
    assert "poll-interval" in capsys.readouterr().err


def test_run_returns_two_on_bad_since(tmp_path, capsys):
    from types import SimpleNamespace

    args = SimpleNamespace(
        db=str(tmp_path / "tail-bad-since.db"),
        task=None,
        since="not-a-window",
        poll_interval_ms=200,
        max_iterations=1,
    )
    rc = tail_cli.run(args)
    assert rc == 2
    assert "invalid --since" in capsys.readouterr().err
