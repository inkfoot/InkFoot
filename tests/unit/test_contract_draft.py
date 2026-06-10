"""Tests for ``inkfoot contract draft`` history-based generation."""

from __future__ import annotations

import pytest

from inkfoot.contracts.draft import (
    DraftError,
    build_draft,
    collect_run_facts,
    parse_window,
)
from inkfoot.contracts.loader import load_contract
from inkfoot.storage.sqlite import SQLiteStorage

_NOW_MS = 1_700_000_000_000


def _storage() -> SQLiteStorage:
    s = SQLiteStorage(path=":memory:")
    s.connect()
    return s


def _seed_run(
    storage: SQLiteStorage,
    run_id: str,
    *,
    task: str,
    nanodollars: int,
    llm_calls: int,
    input_tokens: int = 1000,
    cache_read: int = 700,
    cache_creation: int = 0,
    outcome: str = "success",
    started_at: int = _NOW_MS,
) -> None:
    conn = storage._conn()
    conn.execute(
        """
        INSERT INTO runs (
            id, task, started_at, status, outcome,
            total_input_tokens, total_cache_read_tokens,
            total_cache_creation_tokens, total_nanodollars
        ) VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?)
        """,
        [
            run_id, task, started_at, outcome,
            input_tokens, cache_read, cache_creation, nanodollars,
        ],
    )
    for i in range(llm_calls):
        conn.execute(
            """
            INSERT INTO events (id, run_id, kind, occurred_at, sequence)
            VALUES (?, ?, 'llm_call', ?, ?)
            """,
            [f"{run_id}-e{i}", run_id, started_at, i],
        )


# ----------------------------------------------------------------------
# Window parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,seconds",
    [("30d", 30 * 86400), ("24h", 24 * 3600), ("90m", 90 * 60), ("45s", 45)],
)
def test_parse_window_valid(raw: str, seconds: int) -> None:
    assert parse_window(raw) == seconds


@pytest.mark.parametrize("raw", ["", "30", "d", "30x", "abc"])
def test_parse_window_invalid(raw: str) -> None:
    with pytest.raises(DraftError):
        parse_window(raw)


# ----------------------------------------------------------------------
# Draft generation
# ----------------------------------------------------------------------


def test_draft_from_history_round_trips_through_loader(tmp_path) -> None:
    storage = _storage()
    for i in range(100):
        _seed_run(
            storage,
            f"run-{i}",
            task="triage",
            nanodollars=40_000_000 + i * 100_000,
            llm_calls=4 + (i % 3),
        )
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    assert len(facts) == 100

    result = build_draft("triage", "30d", facts)
    assert not result.low_confidence
    assert result.outlier_count == 0

    # The generated YAML must load cleanly via the real loader.
    path = tmp_path / "triage.yaml"
    path.write_text(result.yaml_text, encoding="utf-8")
    contract = load_contract(path)
    assert contract.task == "triage"
    assert contract.budget.max_nanodollars > 0
    assert contract.budget.max_llm_calls >= 5


def test_draft_zero_cost_history_still_round_trips(tmp_path) -> None:
    # A task whose entire history bills 0 nanodollars (e.g. unpriced
    # models in local testing) must still draft a contract the loader
    # accepts — max_nanodollars is floored at 1, not emitted as 0.
    storage = _storage()
    for i in range(40):
        _seed_run(
            storage, f"run-{i}", task="triage", nanodollars=0, llm_calls=2
        )
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    result = build_draft("triage", "30d", facts)
    assert _extract_max_nanodollars(result.yaml_text) >= 1

    path = tmp_path / "triage.yaml"
    path.write_text(result.yaml_text, encoding="utf-8")
    contract = load_contract(path)
    assert contract.budget.max_nanodollars >= 1


def test_draft_warns_below_minimum_runs() -> None:
    storage = _storage()
    for i in range(5):
        _seed_run(
            storage, f"run-{i}", task="triage", nanodollars=1_000_000, llm_calls=2
        )
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    result = build_draft("triage", "30d", facts)
    assert result.low_confidence
    assert "WARNING" in result.yaml_text


def test_draft_flags_outliers_in_header_not_budget() -> None:
    storage = _storage()
    for i in range(40):
        _seed_run(
            storage, f"run-{i}", task="triage", nanodollars=1_000_000, llm_calls=2
        )
    # A single pathological run 50x the median.
    _seed_run(
        storage, "outlier", task="triage", nanodollars=50_000_000, llm_calls=2
    )
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    result = build_draft("triage", "30d", facts)
    assert result.outlier_count == 1
    assert "outlier" in result.yaml_text.lower()
    # The outlier must not inflate the budget: p95 of the kept runs is
    # the steady 1_000_000 cost + 10% headroom, far below 50_000_000.
    contract_max = _extract_max_nanodollars(result.yaml_text)
    assert contract_max < 5_000_000


def test_draft_no_runs_raises() -> None:
    storage = _storage()
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    with pytest.raises(DraftError, match="no completed runs"):
        build_draft("triage", "30d", facts)


def test_draft_excludes_runs_outside_window() -> None:
    storage = _storage()
    _seed_run(
        storage, "recent", task="triage", nanodollars=1_000_000, llm_calls=2
    )
    _seed_run(
        storage,
        "old",
        task="triage",
        nanodollars=1_000_000,
        llm_calls=2,
        started_at=_NOW_MS - 40 * 86400 * 1000,
    )
    facts = collect_run_facts(storage, "triage", parse_window("30d"), now_ms=_NOW_MS)
    assert len(facts) == 1


class _Args:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def test_cli_draft_against_fresh_db_exits_cleanly(tmp_path, capsys) -> None:
    # A brand-new DB has its migrations applied by run_draft's
    # storage.connect(), so the empty-history path surfaces a clean
    # DraftError (exit 2) instead of a raw "no such table: runs" traceback.
    from inkfoot.cli.contract import run_draft

    db_path = tmp_path / "fresh.db"
    rc = run_draft(_Args(task="triage", window="30d", db=str(db_path), output=None))
    assert rc == 2
    assert "no completed runs" in capsys.readouterr().err


def _extract_max_nanodollars(yaml_text: str) -> int:
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("max_nanodollars:"):
            value = stripped.split(":", 1)[1].split("#", 1)[0].strip()
            return int(value)
    raise AssertionError("max_nanodollars not found in draft")
