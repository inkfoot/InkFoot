"""Pattern B ergonomic helpers (``tag_node`` + ``checkpoint``).

Pattern B is the raw-SDK integration shape: ``@agent_run`` already
provides run scoping; these helpers make raw-SDK instrumentation the
canonical no-framework path:

* :func:`inkfoot.tag_node` — manual analogue of LangGraph per-node
  attribution. Sets ``InMemoryRunState.node_name`` so the *next*
  LLM call's translator stamps it onto
  ``NeutralCall.metadata["node_name"]``.
* :func:`inkfoot.checkpoint` — emits a ``checkpoint`` event so
  reports can compute time deltas between named checkpoints.

The current ``@agent_run`` decorator stays unchanged; these tests
cover the new surface + assert the integration with the existing
run-lifecycle ContextVar.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import inkfoot
from inkfoot._run_context import _clear_current_run, current_run_id
from inkfoot._run_lifecycle import NoActiveRun, checkpoint, tag_node


@pytest.fixture()
def instrumented(tmp_path: Path) -> Any:
    """Fresh instrument()/shutdown() bracket per test — keeps the
    process-global :func:`inkfoot.instrument` state clean."""
    from inkfoot._instrument import shutdown
    from inkfoot.storage.sqlite import SQLiteStorage

    db_path = tmp_path / "runs.db"
    inkfoot.instrument(
        sdks=[],  # don't try to patch SDKs in tests
        storage=SQLiteStorage(path=db_path),
    )
    yield db_path
    _clear_current_run()
    shutdown()


# ----------------------------------------------------------------------
# tag_node
# ----------------------------------------------------------------------


def test_tag_node_sets_node_name_on_in_memory_state(
    instrumented: Path,
) -> None:
    from inkfoot._run_context import get_or_create_run_state

    with inkfoot.agent_run(task="t"):
        run_id = current_run_id()
        assert run_id is not None
        inkfoot.tag_node("retrieval")
        state = get_or_create_run_state(run_id)
        assert state.node_name == "retrieval"


def test_tag_node_persists_until_overwritten(instrumented: Path) -> None:
    from inkfoot._run_context import get_or_create_run_state

    with inkfoot.agent_run(task="t"):
        run_id = current_run_id()
        assert run_id is not None
        inkfoot.tag_node("retrieval")
        inkfoot.tag_node("synthesis")
        state = get_or_create_run_state(run_id)
        assert state.node_name == "synthesis"


def test_tag_node_clears_with_none_or_empty_string(
    instrumented: Path,
) -> None:
    from inkfoot._run_context import get_or_create_run_state

    with inkfoot.agent_run(task="t"):
        run_id = current_run_id()
        inkfoot.tag_node("retrieval")
        inkfoot.tag_node(None)
        state = get_or_create_run_state(run_id)
        assert state.node_name is None

        inkfoot.tag_node("synthesis")
        inkfoot.tag_node("")
        state = get_or_create_run_state(run_id)
        assert state.node_name is None


def test_tag_node_rejects_non_string_value(instrumented: Path) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(TypeError, match="name must be str"):
            tag_node(42)  # type: ignore[arg-type]


def test_tag_node_outside_run_raises_no_active_run() -> None:
    _clear_current_run()
    with pytest.raises(NoActiveRun, match="agent_run"):
        tag_node("retrieval")


def test_tag_node_flows_into_translator_metadata(
    instrumented: Path,
) -> None:
    """The end-to-end semantic: after ``tag_node('retrieval')``, a
    translator running under the same run sees the value on
    :class:`InMemoryRunState` and stamps it onto
    :attr:`NeutralCall.metadata`."""
    from dataclasses import asdict

    from inkfoot._run_context import get_or_create_run_state
    from inkfoot.normalise.openai import OpenAITranslator
    from inkfoot.run import InMemoryRunState

    with inkfoot.agent_run(task="t"):
        run_id = current_run_id()
        inkfoot.tag_node("retrieval")
        state = get_or_create_run_state(run_id)
        # Minimal request/response so the translator runs cleanly.
        call = OpenAITranslator().translate(
            request={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
            response={
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                }
            },
            run_state=state,
            started_at=0,
            ended_at=1,
        )
        assert call.metadata.get("node_name") == "retrieval"
        # asdict (the storage-path serialiser) preserves it.
        assert asdict(call)["metadata"]["node_name"] == "retrieval"


# ----------------------------------------------------------------------
# checkpoint
# ----------------------------------------------------------------------


def test_checkpoint_emits_event_with_label(instrumented: Path) -> None:
    from inkfoot.storage.sqlite import SQLiteStorage

    with inkfoot.agent_run(task="t") as run:
        inkfoot.checkpoint("after-vector-search")
        run_id = run.id

    # Re-open the DB out-of-band to read what was written.
    s = SQLiteStorage(path=instrumented)
    s.connect()
    try:
        events = [
            ev for ev in s.iter_events(run_id) if ev.get("kind") == "checkpoint"
        ]
    finally:
        s.close()
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload == {"label": "after-vector-search"}


def test_checkpoint_deltas_are_computable(instrumented: Path) -> None:
    """Two checkpoints in sequence let a report subtract their
    timestamps to get a duration."""
    from inkfoot.storage.sqlite import SQLiteStorage

    with inkfoot.agent_run(task="t") as run:
        inkfoot.checkpoint("start")
        time.sleep(0.005)  # ≥ 1 ms apart so the ms-resolution
        # timestamps are reliably distinct on every platform.
        inkfoot.checkpoint("end")
        run_id = run.id

    s = SQLiteStorage(path=instrumented)
    s.connect()
    try:
        evs = [
            ev for ev in s.iter_events(run_id) if ev.get("kind") == "checkpoint"
        ]
    finally:
        s.close()
    assert len(evs) == 2
    delta_ms = int(evs[1]["occurred_at"]) - int(evs[0]["occurred_at"])
    assert delta_ms >= 0  # monotonic
    # Labels round-trip in order.
    labels = [json.loads(ev["payload_json"])["label"] for ev in evs]
    assert labels == ["start", "end"]


def test_checkpoint_rejects_empty_label(instrumented: Path) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(ValueError, match="non-empty"):
            checkpoint("")
        with pytest.raises(ValueError, match="non-empty"):
            checkpoint("   ")  # whitespace-only


def test_checkpoint_rejects_non_string_label(instrumented: Path) -> None:
    with inkfoot.agent_run(task="t"):
        with pytest.raises(TypeError, match="label must be str"):
            checkpoint(42)  # type: ignore[arg-type]


def test_checkpoint_outside_run_raises_no_active_run() -> None:
    _clear_current_run()
    with pytest.raises(NoActiveRun, match="agent_run"):
        checkpoint("stage")


# ----------------------------------------------------------------------
# Top-level re-export contract
# ----------------------------------------------------------------------


def test_top_level_package_exports_tag_node_and_checkpoint() -> None:
    assert inkfoot.tag_node is tag_node
    assert inkfoot.checkpoint is checkpoint
    assert "tag_node" in inkfoot.__all__
    assert "checkpoint" in inkfoot.__all__
