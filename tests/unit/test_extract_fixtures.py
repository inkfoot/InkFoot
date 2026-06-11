"""Tests for ``scripts/extract_run_fixtures.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "extract_run_fixtures.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "extract_run_fixtures", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_run_fixtures"] = module
    spec.loader.exec_module(module)
    return module


_extract = _load_script_module()


def _seed(db_path: Path, *, capture_mode: str = "metadata") -> None:
    """Lay down two llm_call events on one run (one with content,
    one without)."""
    from inkfoot.storage.sqlite import SQLiteStorage

    s = SQLiteStorage(path=db_path)
    s.connect()
    s.start_run(
        run_id="r1",
        task="bench",
        agent_kind="t",
        started_at=1_700_000_000_000,
    )
    s.insert_event(
        event_id="e1",
        run_id="r1",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json=json.dumps(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "ledger": {"output_tokens": 5},
                "tools_called": ["search"],
                "cache_status": "hit",
            }
        ),
        capture_mode=capture_mode,
        request_json=(
            json.dumps({"messages": [{"role": "user", "content": "hi"}]})
            if capture_mode == "replay"
            else None
        ),
        response_json=(
            json.dumps({"usage": {"output_tokens": 5}})
            if capture_mode == "replay"
            else None
        ),
    )
    s.insert_event(
        event_id="e2",
        run_id="r1",
        kind="llm_call",
        occurred_at=1_700_000_000_002,
        sequence=2,
        payload_json=json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "ledger": {"output_tokens": 6},
            }
        ),
    )
    s.close()


def test_parse_since_relative_duration() -> None:
    import time

    now_ms = int(time.time() * 1000)
    out = _extract._parse_since("1h")
    # Inside a 1-second window of (now - 1h).
    assert abs(out - (now_ms - 3600_000)) < 1_000


def test_parse_since_iso_date() -> None:
    out = _extract._parse_since("2026-01-01")
    # 2026-01-01 UTC midnight in ms.
    import datetime as dt

    expected = int(
        dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    assert out == expected


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="since"):
        _extract._parse_since("yesterday")


def test_extract_writes_one_file_per_llm_call(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed(db)
    output = tmp_path / "fixtures"
    count = _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=0,
        include_content=False,
    )
    assert count == 2
    files = sorted(output.glob("*.json"))
    assert len(files) == 2


def test_extract_excludes_old_events(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seed(db)
    output = tmp_path / "fixtures"
    # Cutoff after our seed events (which are at 1_700_000_000_001/2).
    count = _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=1_700_000_000_010,
        include_content=False,
    )
    assert count == 0


def test_metadata_mode_extract_omits_content(tmp_path: Path) -> None:
    """Privacy-first: without --include-content the fixture's
    request/response slots are empty even if the DB has content
    rows."""
    db = tmp_path / "runs.db"
    _seed(db, capture_mode="replay")
    output = tmp_path / "fixtures"
    _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=0,
        include_content=False,
    )
    files = sorted(output.glob("anthropic-*.json"))
    assert files
    data = json.loads(files[0].read_text())
    assert data["request"] == {}
    assert data["response"] == {}


def test_include_content_pulls_request_response_from_event_contents(
    tmp_path: Path,
) -> None:
    db = tmp_path / "runs.db"
    _seed(db, capture_mode="replay")
    output = tmp_path / "fixtures"
    _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=0,
        include_content=True,
    )
    files = sorted(output.glob("anthropic-*.json"))
    assert files
    data = json.loads(files[0].read_text())
    assert data["request"] == {
        "messages": [{"role": "user", "content": "hi"}]
    }
    assert data["response"] == {"usage": {"output_tokens": 5}}


def test_fixture_filename_carries_provider_model_run_sequence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "runs.db"
    _seed(db)
    output = tmp_path / "fixtures"
    _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=0,
        include_content=False,
    )
    names = {p.name for p in output.glob("*.json")}
    # One Anthropic + one OpenAI; sequence-suffixed.
    assert any("anthropic-claude-sonnet-4-6" in n for n in names)
    assert any("openai-gpt-4o" in n for n in names)
    assert any("seq0001" in n for n in names)
    assert any("seq0002" in n for n in names)


def test_filename_uses_full_run_id_avoiding_millisecond_collisions(
    tmp_path: Path,
) -> None:
    """Two distinct ULIDs that share their millisecond-derived
    prefix must not produce colliding filenames. The
    full run id rides in the name; only the ULID's random suffix
    differs between concurrently-started runs."""
    from inkfoot.storage.sqlite import SQLiteStorage

    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    # Two ULIDs that share the first 14 chars (timestamp-derived
    # prefix). The pre-fix filename truncated to 14 → collision.
    shared_prefix = "01ABCDEFGHIJKL"
    run_ids = [shared_prefix + "MNOPQRSTUV", shared_prefix + "WXYZ012345"]
    for rid in run_ids:
        s.start_run(
            run_id=rid,
            task="t",
            agent_kind="a",
            started_at=1_700_000_000_000,
        )
        s.insert_event(
            event_id=f"e-{rid}",
            run_id=rid,
            kind="llm_call",
            occurred_at=1_700_000_000_001,
            sequence=1,
            payload_json=json.dumps(
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "ledger": {"output_tokens": 1},
                }
            ),
        )
    s.close()

    output = tmp_path / "fixtures"
    count = _extract.extract(
        db_path=db,
        output_dir=output,
        since_ms=0,
        include_content=False,
    )
    assert count == 2
    files = sorted(output.glob("*.json"))
    assert len(files) == 2, (
        f"expected two files, got {[f.name for f in files]}"
    )


def test_complete_only_skips_in_progress_runs(tmp_path: Path) -> None:
    """``--complete-only`` filters out runs whose status is still
    'running' — useful for the nightly cron that wants only
    finished runs."""
    from inkfoot.storage.sqlite import SQLiteStorage

    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()

    # Run 1: completed.
    s.start_run(
        run_id="r-complete",
        task="t",
        agent_kind="a",
        started_at=1_700_000_000_000,
    )
    s.insert_event(
        event_id="e-1",
        run_id="r-complete",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json=json.dumps(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "ledger": {"output_tokens": 1},
            }
        ),
    )
    s.end_run(
        run_id="r-complete", ended_at=1_700_000_000_500, status="complete"
    )

    # Run 2: still running.
    s.start_run(
        run_id="r-running",
        task="t",
        agent_kind="a",
        started_at=1_700_000_000_000,
    )
    s.insert_event(
        event_id="e-2",
        run_id="r-running",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json=json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "ledger": {"output_tokens": 2},
            }
        ),
    )
    s.close()

    # Default mode: both runs extracted.
    output_all = tmp_path / "fixtures_all"
    n_all = _extract.extract(
        db_path=db,
        output_dir=output_all,
        since_ms=0,
        include_content=False,
        complete_only=False,
    )
    assert n_all == 2

    # complete_only mode: only the finished run.
    output_complete = tmp_path / "fixtures_complete"
    n_complete = _extract.extract(
        db_path=db,
        output_dir=output_complete,
        since_ms=0,
        include_content=False,
        complete_only=True,
    )
    assert n_complete == 1
    files = list(output_complete.glob("*.json"))
    assert all("r-complete" in p.name for p in files)
