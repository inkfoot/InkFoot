"""LangGraph end-to-end integration test.

Runs a small **real** ``langgraph.StateGraph`` through Inkfoot's
adapter and asserts the event sequence + per-node attribution
matches what the docs promise.

The test skips cleanly when ``langgraph`` isn't installed
(``inkfoot[langgraph]`` extra is optional). On CI, the
``benchmark`` / ``attribution-validation`` matrix doesn't pull
``langgraph``; a separate framework-matrix job
will run this one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "langgraph",
    reason="LangGraph not installed — install inkfoot[langgraph] to run the E2E test.",
)


@pytest.fixture()
def instrumented(tmp_path: Path) -> Any:
    """Boot Inkfoot against a per-test SQLite DB and tear down on
    exit."""
    import inkfoot
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.adapters._registry import AdapterRegistry
    from inkfoot.storage.sqlite import SQLiteStorage

    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    shutdown()


def _build_two_node_graph() -> Any:
    """Build a minimal two-node ``StateGraph`` using a TypedDict
    state. Mirrors the shape a real LangGraph tutorial would have
    (no LLM calls — those are unit-tested separately with stubs)."""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict, total=False):
        input: str
        chunks: list[str]
        answer: str

    def retrieve(state: State) -> State:
        return {"chunks": ["a", "b", "c"]}

    def synthesise(state: State) -> State:
        return {"answer": "result-" + state.get("input", "")}

    sg = StateGraph(State)
    sg.add_node("retrieve", retrieve)
    sg.add_node("synthesise", synthesise)
    sg.add_edge(START, "retrieve")
    sg.add_edge("retrieve", "synthesise")
    sg.add_edge("synthesise", END)
    return sg.compile()


def test_langgraph_two_node_run_emits_expected_event_sequence(
    instrumented: Path,
) -> None:
    from inkfoot._instrument import _STORAGE
    from inkfoot.langgraph import instrument as lg_instrument

    graph = _build_two_node_graph()
    lg_instrument(graph, task="e2e-test")

    out = graph.invoke({"input": "ping"})
    assert out["answer"] == "result-ping"
    assert out["chunks"] == ["a", "b", "c"]

    # One run row, completed.
    rows = list(_STORAGE._conn().execute("SELECT id, task, status FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "e2e-test"
    assert rows[0]["status"] == "complete"

    # Event sequence: run_start, node_enter retrieve, node_exit
    # retrieve, node_enter synthesise, node_exit synthesise, run_end.
    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT kind, payload_json FROM events ORDER BY sequence"
    )
    events = list(cur.fetchall())
    kinds = [ev["kind"] for ev in events]
    assert "run_start" in kinds
    assert "run_end" in kinds

    node_events = [
        ev
        for ev in events
        if ev["kind"] in {"node_enter", "node_exit"}
    ]
    node_pairs = [
        (ev["kind"], json.loads(ev["payload_json"])["node_name"])
        for ev in node_events
    ]
    assert node_pairs == [
        ("node_enter", "retrieve"),
        ("node_exit", "retrieve"),
        ("node_enter", "synthesise"),
        ("node_exit", "synthesise"),
    ]


def test_langgraph_run_completes_cleanly_when_no_llm_calls_happen(
    instrumented: Path,
) -> None:
    """No LLM call inside the nodes is fine — the run still
    completes and the only ledger-relevant events are the node
    enter/exit pairs."""
    from inkfoot._instrument import _STORAGE
    from inkfoot.langgraph import instrument as lg_instrument

    graph = _build_two_node_graph()
    lg_instrument(graph, task="no-llm")

    graph.invoke({"input": "x"})

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='llm_call'"
    )
    n = cur.fetchone()["n"]
    assert n == 0


def test_repeated_invoke_reuses_adapter_idempotently(
    instrumented: Path,
) -> None:
    from inkfoot._instrument import _STORAGE
    from inkfoot.adapters.langgraph import instrument as lg_instrument

    graph = _build_two_node_graph()
    inst1 = lg_instrument(graph, task="reuse")
    inst2 = lg_instrument(graph, task="reuse")
    assert inst1 is inst2  # idempotent

    graph.invoke({"input": "1"})
    graph.invoke({"input": "2"})

    rows = list(_STORAGE._conn().execute("SELECT id FROM runs"))  # type: ignore[union-attr]
    # Two invocations → two distinct runs.
    assert len(rows) == 2
