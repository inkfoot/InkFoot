"""inkfoot tag CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from inkfoot.cli import tag as tag_cli
from inkfoot.storage.sqlite import SQLiteStorage


def _seed_run(db_path: Path, run_id: str = "01HZX") -> None:
    s = SQLiteStorage(path=db_path)
    s.connect()
    try:
        s.start_run(
            run_id=run_id,
            task="t",
            agent_kind="test",
            started_at=1_700_000_000_000,
        )
        s.insert_event(
            event_id="e1",
            run_id=run_id,
            kind="llm_call",
            occurred_at=1_700_000_000_100,
            sequence=1,
            payload_json="{}",
        )
    finally:
        s.close()


def _run_args(db: Path, run_id: str, key: str, value: str) -> SimpleNamespace:
    return SimpleNamespace(db=db, run_id=run_id, key=key, value=value)


def _read_tags(db_path: Path, run_id: str) -> list[dict]:
    s = SQLiteStorage(path=db_path)
    s.connect()
    try:
        return [
            e for e in s.iter_events(run_id) if e["kind"] == "user_tag"
        ]
    finally:
        s.close()


def test_tag_persists_user_tag_event(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed_run(db)
    rc = tag_cli.run(_run_args(db, "01HZX", "region", "us-east"))
    assert rc == 0
    tags = _read_tags(db, "01HZX")
    assert len(tags) == 1
    payload = json.loads(tags[0]["payload_json"])
    assert payload == {"key": "region", "value": "us-east"}


def test_tag_parses_numeric_value_as_int(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed_run(db)
    rc = tag_cli.run(_run_args(db, "01HZX", "retries", "5"))
    assert rc == 0
    tags = _read_tags(db, "01HZX")
    payload = json.loads(tags[0]["payload_json"])
    assert payload["value"] == 5
    assert isinstance(payload["value"], int)


def test_tag_parses_bool_value(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed_run(db)
    rc = tag_cli.run(_run_args(db, "01HZX", "is_prod", "true"))
    assert rc == 0
    tags = _read_tags(db, "01HZX")
    payload = json.loads(tags[0]["payload_json"])
    assert payload["value"] is True


def test_tag_falls_back_to_string_when_value_not_json(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed_run(db)
    rc = tag_cli.run(_run_args(db, "01HZX", "name", "alice"))
    assert rc == 0
    tags = _read_tags(db, "01HZX")
    payload = json.loads(tags[0]["payload_json"])
    assert payload["value"] == "alice"


def test_tag_returns_nonzero_for_unknown_run(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    # Don't seed.
    s = SQLiteStorage(path=db)
    s.connect()
    s.close()  # Just create the schema.
    rc = tag_cli.run(_run_args(db, "missing", "k", "v"))
    assert rc != 0


def test_tag_appends_after_existing_events_max_sequence(tmp_path: Path) -> None:
    """Late tagging on a run with existing events lands with a
    sequence number strictly greater than the prior max — so
    ORDER BY sequence ASC still produces a sensible chronological
    listing in inkfoot report."""
    db = tmp_path / "runs.db"
    _seed_run(db, "01HZX")
    rc = tag_cli.run(_run_args(db, "01HZX", "k", "v"))
    assert rc == 0
    s = SQLiteStorage(path=db)
    s.connect()
    try:
        events = list(s.iter_events("01HZX"))
        kinds = [e["kind"] for e in events]
        assert kinds[-1] == "user_tag"
        # The user_tag's sequence is > the llm_call's sequence.
        assert events[-1]["sequence"] > events[-2]["sequence"]
    finally:
        s.close()


def test_tag_flips_aggregates_dirty(tmp_path: Path) -> None:
    """The tag event must trigger the aggregator's dirty-flag flow
    so the next aggregator pass picks up the run."""
    db = tmp_path / "runs.db"
    _seed_run(db, "01HZX")
    # Manually clear the dirty flag so we can verify tag flips it back.
    s = SQLiteStorage(path=db)
    s.connect()
    s._conn().execute(
        "UPDATE runs SET aggregates_dirty = 0 WHERE id = '01HZX'"
    )
    s.close()

    rc = tag_cli.run(_run_args(db, "01HZX", "k", "v"))
    assert rc == 0

    s2 = SQLiteStorage(path=db)
    s2.connect()
    try:
        row = s2.get_run("01HZX")
        assert row["aggregates_dirty"] == 1
    finally:
        s2.close()
