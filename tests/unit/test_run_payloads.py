"""Tests for the event payload dataclasses.

Covers:
- ``Run`` field set matches the SQLite ``runs`` table 1:1
  (reflection test against the live migration).
- ``NeutralCall`` is frozen and round-trips through
  ``dataclasses.asdict`` → ``dict_to_neutral_call`` losslessly.
- ``InMemoryRunState`` has no DB-persistence helpers — pinned by an
  assertion on its attribute surface.
- ``update_stable_prefix`` shortens monotonically and handles the
  empty-string / non-string edges.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import (
    NeutralCall,
    NeutralError,
    dict_to_neutral_call,
    update_stable_prefix,
)
from inkfoot.run import RUN_TABLE_COLUMNS, InMemoryRunState, Run
from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# Run <-> SQLite schema reflection
# ----------------------------------------------------------------------


def test_run_dataclass_fields_match_run_table_columns(tmp_path: Path) -> None:
    """Open a fresh DB, apply migrations, read the runs table's
    column list, and assert the Run dataclass mirrors it 1:1."""
    s = SQLiteStorage(path=tmp_path / "runs.db")
    s.connect()
    try:
        conn = s._conn()
        rows = conn.execute("PRAGMA table_info(runs)").fetchall()
    finally:
        s.close()
    sqlite_cols = tuple(row["name"] for row in rows)
    dc_fields = tuple(f.name for f in fields(Run))
    assert dc_fields == sqlite_cols, (
        f"Run dataclass fields {dc_fields} "
        f"don't match SQLite runs columns {sqlite_cols}"
    )


def test_run_table_columns_constant_matches_dataclass() -> None:
    dc_fields = tuple(f.name for f in fields(Run))
    assert RUN_TABLE_COLUMNS == dc_fields


def test_run_is_frozen() -> None:
    run = Run(id="r1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.task = "x"  # type: ignore[misc]


# ----------------------------------------------------------------------
# NeutralCall round-trip
# ----------------------------------------------------------------------


def _sample_call() -> NeutralCall:
    return NeutralCall(
        provider="anthropic",
        model="claude-sonnet-4-6",
        started_at=1_700_000_000_000,
        ended_at=1_700_000_000_500,
        ledger=CausalTokenLedger(
            user_input_tokens=10, output_tokens=5
        ),
        estimated_nanodollars=12_345,
        tools_offered=("get_weather", "lookup"),
        tools_called=("get_weather",),
        error=None,
        cache_status="hit",
        parent_run_id="parent-run",
        sequence=7,
        estimation_flags=("system_static_tokens",),
    )


def test_neutral_call_is_frozen() -> None:
    call = _sample_call()
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.sequence = 99  # type: ignore[misc]


def test_neutral_call_round_trips_via_asdict() -> None:
    original = _sample_call()
    payload = asdict(original)
    rebuilt = dict_to_neutral_call(payload)
    assert rebuilt == original


def test_neutral_call_with_error_round_trips() -> None:
    original = dataclasses.replace(
        _sample_call(),
        error=NeutralError(
            type="RateLimitError", message="too many", retryable=True
        ),
    )
    rebuilt = dict_to_neutral_call(asdict(original))
    assert rebuilt == original


def test_dict_to_neutral_call_rejects_unknown_top_level_keys() -> None:
    payload = asdict(_sample_call())
    payload["sneaky"] = "value"
    with pytest.raises(ValueError, match="unknown keys"):
        dict_to_neutral_call(payload)


def test_dict_to_neutral_call_rejects_unknown_ledger_keys() -> None:
    payload = asdict(_sample_call())
    payload["ledger"]["unknown_field"] = 5
    with pytest.raises(ValueError, match="unknown ledger keys"):
        dict_to_neutral_call(payload)


def test_dict_to_neutral_call_rejects_invalid_cache_status() -> None:
    payload = asdict(_sample_call())
    payload["cache_status"] = "yes please"
    with pytest.raises(ValueError, match="cache_status"):
        dict_to_neutral_call(payload)


def test_neutral_call_constructor_validates_cache_status() -> None:
    """Constructor and deserialiser enforce the same contract — a
    translator constructing NeutralCall(cache_status='weird', ...)
    directly must hit the same error path."""
    with pytest.raises(ValueError, match="cache_status"):
        NeutralCall(
            provider="anthropic",
            model="claude-sonnet-4-6",
            started_at=0,
            ended_at=1,
            ledger=CausalTokenLedger(),
            cache_status="weird",
        )


def test_neutral_call_constructor_accepts_all_four_cache_status_values() -> None:
    for status in ("hit", "partial", "miss", "n/a"):
        call = NeutralCall(
            provider="anthropic",
            model="claude-sonnet-4-6",
            started_at=0,
            ended_at=1,
            ledger=CausalTokenLedger(),
            cache_status=status,
        )
        assert call.cache_status == status


def test_dict_to_neutral_call_requires_ledger() -> None:
    payload = asdict(_sample_call())
    del payload["ledger"]
    with pytest.raises(ValueError, match="ledger"):
        dict_to_neutral_call(payload)


def test_dict_to_neutral_call_accepts_neutral_objects_directly() -> None:
    payload = asdict(_sample_call())
    payload["ledger"] = CausalTokenLedger(user_input_tokens=10, output_tokens=5)
    rebuilt = dict_to_neutral_call(payload)
    assert rebuilt.ledger.user_input_tokens == 10


# ----------------------------------------------------------------------
# InMemoryRunState
# ----------------------------------------------------------------------


def test_in_memory_run_state_has_no_storage_helpers() -> None:
    """Tightly pin the public attribute surface so a future
    contributor can't quietly add a ``save`` / ``load`` helper that
    would conflict with the architecture's "not persisted"
    contract."""
    state = InMemoryRunState()
    public = {n for n in dir(state) if not n.startswith("_")}
    expected = {
        "stable_system_prefix",
        "recent_calls",
        "retry_counts",
        # The run lifecycle added a pre-call token counter for inkfoot.tag_retrieval.
        # Process-local; not persisted.
        "pending_retrieved_context_tokens",
        # Cache-resource providers (Gemini): set by the cache-resource
        # arm of CacheControlPlacer right after it creates a provider-
        # side cache resource; the translator re-attributes that
        # call's cached count to cache_creation_tokens and resets the
        # flag. Process-local; not persisted.
        "pending_cache_resource_creation",
        # the adapter work (framework metadata contract): adapter / tag_node-supplied node name
        # the translator stamps onto NeutralCall.metadata["node_name"].
        # Process-local; not persisted.
        "node_name",
        # the adapter work: LangGraph adapter snapshots a stable fingerprint
        # of the compiled graph's tools array. Process-local; not
        # persisted.
        "tools_fingerprint",
        # Multi-agent attribution: the CrewAI adapter scopes these
        # around per-agent / per-framework-task execution so the
        # translator stamps metadata["agent_name"] / ["task_name"].
        # Process-local; not persisted.
        "agent_name",
        "task_name",
    }
    assert public == expected, (
        f"InMemoryRunState public surface drifted: {public ^ expected}"
    )


def test_in_memory_run_state_is_not_referenced_by_storage_modules() -> None:
    """``InMemoryRunState`` must never appear in storage code — that
    would re-couple the lifecycle the split intentionally separated."""
    import inkfoot.storage as storage_pkg
    import inkfoot.storage.sqlite as sqlite_mod
    import inkfoot.storage.aggregator as agg_mod
    import inkfoot.storage.migrations as mig_mod

    for module in (storage_pkg, sqlite_mod, agg_mod, mig_mod):
        src = open(module.__file__).read()
        assert "InMemoryRunState" not in src, (
            f"InMemoryRunState appears in {module.__name__} — it must "
            "stay process-local and out of the storage layer"
        )


def test_in_memory_run_state_defaults() -> None:
    state = InMemoryRunState()
    assert state.stable_system_prefix == ""
    assert state.recent_calls == []
    assert state.retry_counts == {}


def test_in_memory_run_state_is_mutable() -> None:
    state = InMemoryRunState()
    state.stable_system_prefix = "abc"
    state.recent_calls.append("call")
    state.retry_counts["err"] = 1
    assert state.stable_system_prefix == "abc"
    assert state.recent_calls == ["call"]
    assert state.retry_counts == {"err": 1}


# ----------------------------------------------------------------------
# update_stable_prefix
# ----------------------------------------------------------------------


def test_update_stable_prefix_seeds_on_first_call() -> None:
    assert update_stable_prefix("", "You are a helpful agent.") == (
        "You are a helpful agent."
    )


def test_update_stable_prefix_shortens_at_divergence() -> None:
    out = update_stable_prefix(
        "You are an agent. Date: 2026-05-25",
        "You are an agent. Date: 2026-05-26",
    )
    assert out == "You are an agent. Date: 2026-05-2"


def test_update_stable_prefix_collapses_to_empty_on_total_change() -> None:
    out = update_stable_prefix("hello", "world")
    assert out == ""


def test_update_stable_prefix_never_grows() -> None:
    prefix = "shorter"
    out = update_stable_prefix(prefix, "shorter and then more")
    assert out == prefix
    assert len(out) <= len(prefix)


def test_update_stable_prefix_empty_new_block_collapses() -> None:
    assert update_stable_prefix("any prefix", "") == ""


def test_update_stable_prefix_rejects_non_strings() -> None:
    with pytest.raises(TypeError):
        update_stable_prefix(None, "hello")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        update_stable_prefix("hello", None)  # type: ignore[arg-type]
