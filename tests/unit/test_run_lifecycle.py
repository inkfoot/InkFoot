"""Tests for ``inkfoot.agent_run`` + ``set_outcome`` / ``tag`` /
``tag_retrieval`` / ``report_cost`` .

The run-lifecycle code touches storage, so each test sets up a
real :class:`SQLiteStorage` (in-memory or tempfile) via
``inkfoot.instrument`` so the ContextVar plumbing matches
production. The shim isn't installed; events come from agent_run
itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
import inkfoot._run_lifecycle as run_lifecycle
from inkfoot._run_context import _clear_current_run, _reset_ambient_run, current_run_id
from inkfoot._run_lifecycle import NoActiveRun
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage


@pytest.fixture
def instrumented(tmp_path: Path):
    """Spin up a clean storage + instrumentation per test."""
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    yield storage
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()


def _events(storage: SQLiteStorage, run_id: str) -> list[dict[str, Any]]:
    return list(storage.iter_events(run_id))


# ----------------------------------------------------------------------
# Decorator
# ----------------------------------------------------------------------


def test_decorator_inserts_running_row_and_completes(instrumented) -> None:
    storage = instrumented
    seen_run_id: list[str] = []

    @inkfoot.agent_run(task="t")
    def work() -> int:
        seen_run_id.append(current_run_id() or "")
        return 42

    result = work()
    assert result == 42

    # The handle's run id ended up on the ContextVar inside.
    assert seen_run_id[0].startswith("run-")
    run_id = seen_run_id[0]

    row = storage.get_run(run_id)
    assert row is not None
    assert row["status"] == "complete"
    assert row["task"] == "t"

    kinds = [e["kind"] for e in _events(storage, run_id)]
    assert kinds.count("run_start") == 1
    assert kinds.count("run_end") == 1


def test_decorator_marks_error_on_exception(instrumented) -> None:
    storage = instrumented

    @inkfoot.agent_run(task="boom")
    def work() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        work()

    # Find the only run row.
    conn = storage._conn()
    rows = conn.execute(
        "SELECT id, status FROM runs ORDER BY started_at DESC"
    ).fetchall()
    assert len(rows) >= 1
    run_id = rows[0]["id"]
    assert rows[0]["status"] == "error"

    events = _events(storage, run_id)
    run_end = [e for e in events if e["kind"] == "run_end"]
    assert len(run_end) == 1
    payload = json.loads(run_end[0]["payload_json"])
    assert payload["status"] == "error"
    assert "RuntimeError" in payload["error_message"]


def test_ctx_manager_has_identical_semantics(instrumented) -> None:
    storage = instrumented
    with inkfoot.agent_run(task="ctx") as handle:
        run_id = handle.id
        assert current_run_id() == run_id

    row = storage.get_run(run_id)
    assert row is not None
    assert row["status"] == "complete"
    # After exit, ContextVar restored to None.
    assert current_run_id() is None


def test_ctx_manager_propagates_exception_and_records_error(instrumented) -> None:
    storage = instrumented
    with pytest.raises(ValueError):
        with inkfoot.agent_run(task="ctx-error") as handle:
            run_id = handle.id
            raise ValueError("kaboom")
    row = storage.get_run(run_id)
    assert row["status"] == "error"


def test_manual_start_end_works(instrumented) -> None:
    storage = instrumented
    handle = inkfoot.agent_run(task="manual").start()
    run_id = handle.id
    assert current_run_id() == run_id
    handle.end(status="complete")
    assert current_run_id() is None
    row = storage.get_run(run_id)
    assert row["status"] == "complete"


def test_manual_end_is_idempotent(instrumented) -> None:
    handle = inkfoot.agent_run(task="manual").start()
    handle.end()
    handle.end()  # second call no-ops


def test_nested_runs_have_parent_run_id(instrumented) -> None:
    storage = instrumented
    with inkfoot.agent_run(task="outer") as outer:
        with inkfoot.agent_run(task="inner") as inner:
            # Inner is the current run.
            assert current_run_id() == inner.id
        # Inner exited; outer is current again.
        assert current_run_id() == outer.id

    inner_row = storage.get_run(inner.id)
    outer_row = storage.get_run(outer.id)
    assert inner_row["parent_run_id"] == outer.id
    assert outer_row["parent_run_id"] is None


def test_async_decorator_works_under_asyncio(instrumented) -> None:
    storage = instrumented
    seen: list[str] = []

    @inkfoot.agent_run(task="async-t")
    async def work() -> int:
        seen.append(current_run_id() or "")
        await asyncio.sleep(0)
        return 7

    result = asyncio.run(work())
    assert result == 7
    row = storage.get_run(seen[0])
    assert row["status"] == "complete"


def test_async_ctx_manager_form(instrumented) -> None:
    storage = instrumented

    async def runner() -> str:
        async with inkfoot.agent_run(task="async-ctx") as handle:
            return handle.id

    run_id = asyncio.run(runner())
    row = storage.get_run(run_id)
    assert row["status"] == "complete"


def test_agent_run_without_instrument_raises_clear_error(tmp_path: Path) -> None:
    instrument_mod.shutdown()
    with pytest.raises(inkfoot.InkfootError, match="instrument"):
        with inkfoot.agent_run(task="no-storage"):
            pass


# ----------------------------------------------------------------------
# Per-run state cleanup — no memory leak across runs
# ----------------------------------------------------------------------


def test_clean_exit_releases_per_run_state(instrumented) -> None:
    """``_RunHandle.end`` must drop the run's entry from both
    in-memory dicts (``_sequence_counters`` in
    ``inkfoot.shims._emit`` and ``_run_states`` in
    ``inkfoot._run_context``). Without this a long-lived process
    leaks one dict entry per run forever."""
    from inkfoot._run_context import _run_states
    from inkfoot.shims._emit import _sequence_counters

    sequence_baseline = len(_sequence_counters)
    state_baseline = len(_run_states)

    for _ in range(20):
        with inkfoot.agent_run(task="t") as handle:
            # Touch the per-run state so it actually enters the
            # _run_states map (translators do this in production).
            from inkfoot._run_context import get_or_create_run_state

            get_or_create_run_state(handle.id)
            # Force a sequence allocation too.
            from inkfoot.shims._emit import _next_sequence

            _next_sequence(handle.id)

    # After 20 clean exits the dicts should not have grown.
    assert len(_sequence_counters) == sequence_baseline
    assert len(_run_states) == state_baseline


def test_manual_end_releases_per_run_state(instrumented) -> None:
    """Manual ``run.end()`` (no context manager) also releases."""
    from inkfoot._run_context import _run_states, get_or_create_run_state
    from inkfoot.shims._emit import _next_sequence, _sequence_counters

    handle = inkfoot.agent_run(task="t").start()
    get_or_create_run_state(handle.id)
    _next_sequence(handle.id)
    assert handle.id in _sequence_counters
    assert handle.id in _run_states

    handle.end()
    assert handle.id not in _sequence_counters
    assert handle.id not in _run_states


def test_abandoned_run_cleanup_releases_per_run_state(tmp_path: Path) -> None:
    """``_mark_abandoned_runs`` must also release per-run state.
    An abandoned run by definition never gets a clean ``end()``."""
    from inkfoot._run_context import _run_states, get_or_create_run_state
    from inkfoot.shims._emit import _next_sequence, _sequence_counters

    instrument_mod.shutdown()
    _reset_ambient_run()
    _clear_current_run()
    storage = SQLiteStorage(path=tmp_path / "abandoned.db")
    inkfoot.instrument(storage=storage)

    handle = inkfoot.agent_run(task="leak-then-abandon").start()
    run_id = handle.id
    get_or_create_run_state(run_id)
    _next_sequence(run_id)
    assert run_id in _sequence_counters
    assert run_id in _run_states

    # Simulate process exit without handle.end() — atexit fires
    # shutdown() which calls _mark_abandoned_runs.
    instrument_mod.shutdown()

    assert run_id not in _sequence_counters
    assert run_id not in _run_states


def test_abandoned_runs_marked_error_on_shutdown(tmp_path: Path) -> None:
    """Process exit without ``__exit__`` should flip ``running``
    rows to ``error`` with ``error_message='abandoned'``."""
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()

    storage = SQLiteStorage(path=tmp_path / "abandoned.db")
    inkfoot.instrument(storage=storage)
    handle = inkfoot.agent_run(task="leak").start()
    run_id = handle.id
    # Simulate process exit by calling shutdown() directly without
    # handle.end() — production atexit hook does the same.
    instrument_mod.shutdown()

    # Reopen the DB; the abandoned run should now be in 'error'.
    storage2 = SQLiteStorage(path=tmp_path / "abandoned.db")
    storage2.connect()
    try:
        row = storage2.get_run(run_id)
        assert row is not None
        assert row["status"] == "error"
        # The run_end event carries error_message='abandoned'.
        run_end = [
            e
            for e in storage2.iter_events(run_id)
            if e["kind"] == "run_end"
        ]
        assert run_end
        payload = json.loads(run_end[-1]["payload_json"])
        assert payload["error_message"] == "abandoned"
    finally:
        storage2.close()


# ----------------------------------------------------------------------
# set_outcome
# ----------------------------------------------------------------------


def test_set_outcome_emits_outcome_event(instrumented) -> None:
    storage = instrumented
    with inkfoot.agent_run(task="t") as handle:
        inkfoot.set_outcome("success", quality_score=0.94)
    events = _events(storage, handle.id)
    outcomes = [e for e in events if e["kind"] == "outcome"]
    assert len(outcomes) == 1
    payload = json.loads(outcomes[0]["payload_json"])
    assert payload == {"outcome": "success", "quality_score": 0.94}


@pytest.mark.parametrize(
    "outcome",
    ["success", "accepted_answer", "failure", "human_escalated"],
)
def test_set_outcome_accepts_every_documented_outcome(
    instrumented, outcome
) -> None:
    storage = instrumented
    with inkfoot.agent_run(task="t") as handle:
        inkfoot.set_outcome(outcome)
    events = _events(storage, handle.id)
    payloads = [
        json.loads(e["payload_json"])
        for e in events
        if e["kind"] == "outcome"
    ]
    assert [p["outcome"] for p in payloads] == [outcome]


def test_set_outcome_outside_a_run_raises_clear_error(instrumented) -> None:
    with pytest.raises(NoActiveRun, match="agent_run"):
        inkfoot.set_outcome("success")


def test_set_outcome_rejects_invalid_outcome_name(instrumented) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(ValueError, match="outcome"):
            inkfoot.set_outcome("ok")


def test_set_outcome_rejects_quality_score_out_of_range(instrumented) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(ValueError, match="quality_score"):
            inkfoot.set_outcome("success", quality_score=1.5)
        with pytest.raises(ValueError, match="quality_score"):
            inkfoot.set_outcome("success", quality_score=-0.1)


def test_set_outcome_rejects_non_numeric_quality_score(instrumented) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(TypeError):
            inkfoot.set_outcome("success", quality_score="great")  # type: ignore[arg-type]


def test_set_outcome_accepts_none_quality_score(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        inkfoot.set_outcome("failure", quality_score=None)
    events = _events(instrumented, handle.id)
    outcomes = [e for e in events if e["kind"] == "outcome"]
    payload = json.loads(outcomes[0]["payload_json"])
    assert payload["quality_score"] is None


# ----------------------------------------------------------------------
# tag
# ----------------------------------------------------------------------


def test_tag_emits_user_tag_event(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        inkfoot.tag("user_tier", "enterprise")
    events = _events(instrumented, handle.id)
    tags = [e for e in events if e["kind"] == "user_tag"]
    assert len(tags) == 1
    payload = json.loads(tags[0]["payload_json"])
    assert payload == {"key": "user_tier", "value": "enterprise"}


def test_tag_accepts_all_scalar_types(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        inkfoot.tag("k_str", "value")
        inkfoot.tag("k_int", 5)
        inkfoot.tag("k_float", 0.5)
        inkfoot.tag("k_bool", True)
        inkfoot.tag("k_none", None)
    tags = [e for e in _events(instrumented, handle.id) if e["kind"] == "user_tag"]
    assert len(tags) == 5


def test_tag_rejects_non_scalar_value(instrumented) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(TypeError, match="JSON-scalar"):
            inkfoot.tag("k", {"nested": "dict"})  # type: ignore[arg-type]


def test_tag_rejects_empty_key(instrumented) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(ValueError):
            inkfoot.tag("", "value")


def test_tag_outside_run_raises(instrumented) -> None:
    with pytest.raises(NoActiveRun):
        inkfoot.tag("k", "v")


# ----------------------------------------------------------------------
# tag_retrieval
# ----------------------------------------------------------------------


def test_tag_retrieval_accumulates_pending_tokens(instrumented) -> None:
    from inkfoot._run_context import get_or_create_run_state

    with inkfoot.agent_run(task="t") as handle:
        inkfoot.tag_retrieval("hello world")
        inkfoot.tag_retrieval("more retrieved text")
        state = get_or_create_run_state(handle.id)
        # Two non-empty strings; pending count is non-zero.
        assert state.pending_retrieved_context_tokens > 0


def test_tag_retrieval_empty_string_is_noop(instrumented) -> None:
    from inkfoot._run_context import get_or_create_run_state

    with inkfoot.agent_run(task="t") as handle:
        inkfoot.tag_retrieval("")
        state = get_or_create_run_state(handle.id)
        assert state.pending_retrieved_context_tokens == 0


def test_tag_retrieval_outside_run_raises(instrumented) -> None:
    with pytest.raises(NoActiveRun):
        inkfoot.tag_retrieval("text")


def test_tag_retrieval_lifts_into_next_call_ledger(instrumented) -> None:
    """When a translator runs after tag_retrieval, the pending
    count lands on that call's ledger and the counter resets."""
    from inkfoot._run_context import get_or_create_run_state
    from inkfoot.normalise.anthropic import AnthropicTranslator

    with inkfoot.agent_run(task="t") as handle:
        inkfoot.tag_retrieval("retrieved chunk")
        state = get_or_create_run_state(handle.id)
        before = state.pending_retrieved_context_tokens
        assert before > 0

        translator = AnthropicTranslator()
        call = translator.translate(
            request={
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "x"}],
            },
            response={
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
            run_state=state,
            started_at=0,
            ended_at=1,
        )
        assert call.ledger.retrieved_context_tokens == before
        # And the counter reset.
        assert state.pending_retrieved_context_tokens == 0


# ----------------------------------------------------------------------
# report_cost
# ----------------------------------------------------------------------


def test_report_cost_returns_decimal_zero_when_no_calls(instrumented) -> None:
    from decimal import Decimal

    with inkfoot.agent_run(task="t"):
        cost = inkfoot.report_cost()
    assert cost == Decimal("0")


def test_report_cost_outside_run_raises(instrumented) -> None:
    with pytest.raises(NoActiveRun):
        inkfoot.report_cost()


def test_report_cost_reflects_storage_total(instrumented) -> None:
    storage = instrumented
    with inkfoot.agent_run(task="t") as handle:
        # Directly bump runs.total_nanodollars (simulating an
        # aggregator pass after some llm_call events).
        conn = storage._conn()
        conn.execute(
            "UPDATE runs SET total_nanodollars = ? WHERE id = ?",
            (1_500_000, handle.id),
        )
        cost = inkfoot.report_cost()
    from decimal import Decimal
    assert cost == Decimal("0.001500000")
