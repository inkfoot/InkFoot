"""LangGraph adapter unit tests.

The adapter is **duck-typed** against the LangGraph surface so these
tests use a tiny stub graph rather than spinning up real LangGraph.
The stub mimics the two attributes the adapter touches:

  * ``invoke(state)`` / ``ainvoke(state)`` — entry-point methods the
    adapter monkey-patches to scope an :func:`agent_run`.
  * ``nodes`` — dict mapping ``node_name → callable`` that gets
    in-place wrapping so per-node events fire.

A separate ``tools`` list on the stub exercises the tools-fingerprint
capture path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.langgraph import (
    LangGraphAdapter,
    _compute_tools_fingerprint,
    instrument,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path) -> Any:
    """Boot ``inkfoot.instrument()`` against a fresh DB per test.
    The adapter writes events through the configured storage, so we
    need a real storage backend to assert on what it emitted.

    Resets the module-level default adapter's install count so the
    auto-deactivate test logic (Finding #4) isn't fouled by counter
    leakage between tests that use the convenience ``instrument()``
    function."""
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.adapters.langgraph import _default_adapter
    from inkfoot.storage.sqlite import SQLiteStorage

    _default_adapter._install_count = 0
    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    _default_adapter._install_count = 0
    shutdown()


class _StubGraph:
    """Mimics LangGraph's CompiledStateGraph surface for tests."""

    def __init__(self, *, tools: list[Any] | None = None) -> None:
        self.tools = tools or []
        self.invocations: list[Any] = []
        self.nodes: dict[str, Any] = {
            "retrieve": self._retrieve,
            "synthesise": self._synthesise,
        }
        self.node_call_log: list[str] = []

    # --- node implementations ----------------------------------------

    def _retrieve(self, state: dict[str, Any]) -> dict[str, Any]:
        self.node_call_log.append("retrieve")
        # Simulate an LLM call inside the node so the metadata flows
        # into a real ``llm_call`` event row.
        self._emit_fake_llm_call("retrieve")
        return {"chunks": ["a", "b"]}

    def _synthesise(self, state: dict[str, Any]) -> dict[str, Any]:
        self.node_call_log.append("synthesise")
        self._emit_fake_llm_call("synthesise")
        return {"answer": "done"}

    # --- entry points ------------------------------------------------

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append(state)
        # Execute the nodes through the dict so wrapped callables fire.
        for name, fn in list(self.nodes.items()):
            state = fn(state)
        return state

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.invoke(state)

    # --- helpers -----------------------------------------------------

    def _emit_fake_llm_call(self, _node: str) -> None:
        """Drive the OpenAI translator through the shim's emit path
        so the resulting ``llm_call`` event has real metadata. We
        write the event directly so the test doesn't need the SDK
        installed."""
        from inkfoot._instrument import _STORAGE
        from inkfoot._run_context import (
            current_run_id,
            get_or_create_run_state,
        )
        from inkfoot.normalise.openai import OpenAITranslator
        from inkfoot.shims._emit import _next_sequence
        from ulid import ULID
        from dataclasses import asdict

        run_id = current_run_id()
        assert run_id is not None, "_emit_fake_llm_call needs an active run"
        state = get_or_create_run_state(run_id)
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
        _STORAGE.insert_event(  # type: ignore[union-attr]
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=1,
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(asdict(call), default=str),
            capture_mode="metadata",
        )


# ----------------------------------------------------------------------
# Idempotence + entry-point wrapping
# ----------------------------------------------------------------------


def test_instrument_is_idempotent_returns_same_handle() -> None:
    g = _StubGraph()
    h1 = instrument(g, task="t")
    h2 = instrument(g, task="t")
    assert h1 is h2


def test_invoke_runs_inside_a_scoped_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    g = _StubGraph()
    instrument(g, task="lg-test")
    g.invoke({"input": 1})

    rows = list(_STORAGE._conn().execute("SELECT id, task FROM runs"))  # type: ignore[union-attr]
    # One run for the graph invocation.
    assert len(rows) == 1
    assert rows[0]["task"] == "lg-test"


def test_ainvoke_async_entry_point_works() -> None:
    from inkfoot._instrument import _STORAGE

    g = _StubGraph()
    instrument(g, task="lg-async")
    asyncio.run(g.ainvoke({"input": 1}))

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "lg-async"


# ----------------------------------------------------------------------
# Per-node wrapping
# ----------------------------------------------------------------------


def test_each_node_emits_exactly_one_enter_and_one_exit_event() -> None:
    from inkfoot._instrument import _STORAGE

    g = _StubGraph()
    instrument(g, task="t")
    g.invoke({"input": 1})

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT kind, payload_json FROM events "
        "WHERE kind IN ('node_enter', 'node_exit') ORDER BY sequence"
    )
    events = cur.fetchall()
    # 2 nodes × (enter + exit) = 4.
    assert len(events) == 4
    kinds = [row["kind"] for row in events]
    names = [json.loads(row["payload_json"])["node_name"] for row in events]
    assert kinds == ["node_enter", "node_exit", "node_enter", "node_exit"]
    assert names == ["retrieve", "retrieve", "synthesise", "synthesise"]


def test_llm_call_metadata_carries_node_name() -> None:
    from inkfoot._instrument import _STORAGE

    g = _StubGraph()
    instrument(g, task="t")
    g.invoke({"input": 1})

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call' ORDER BY sequence"
    )
    rows = cur.fetchall()
    assert len(rows) == 2
    metas = [json.loads(r["payload_json"])["metadata"] for r in rows]
    assert metas[0]["node_name"] == "retrieve"
    assert metas[1]["node_name"] == "synthesise"


def test_node_name_restored_after_exit_so_outer_scope_resumes() -> None:
    """Manual ``tag_node`` is set, an instrumented graph runs (which
    stamps its own node_name during execution), and after the
    graph returns the prior ``tag_node`` value is restored."""
    from inkfoot._run_context import get_or_create_run_state

    g = _StubGraph()
    instrument(g, task="t")

    with inkfoot.agent_run(task="t"):
        inkfoot.tag_node("outer")
        run_id = inkfoot._run_context.current_run_id()
        assert run_id is not None

        # Mimic an outer caller invoking the graph inside their own
        # agent_run. The wrapped entry sees the existing run and
        # doesn't open a new one — and the node wrapping should
        # restore ``outer`` when the graph returns.
        g.invoke({"input": 1})

        state = get_or_create_run_state(run_id)
        assert state.node_name == "outer"


# ----------------------------------------------------------------------
# Tools fingerprint
# ----------------------------------------------------------------------


def test_tools_fingerprint_is_stable_for_same_tools() -> None:
    tools = [{"name": "search", "description": "look up"}, {"name": "calc"}]
    fp1 = _compute_tools_fingerprint(tools)
    fp2 = _compute_tools_fingerprint(list(tools))
    assert fp1 == fp2
    assert fp1 and len(fp1) == 16  # configured fingerprint length


def test_tools_fingerprint_differs_when_tools_differ() -> None:
    fp1 = _compute_tools_fingerprint([{"name": "search"}])
    fp2 = _compute_tools_fingerprint(
        [{"name": "search"}, {"name": "calc"}]
    )
    assert fp1 != fp2


def test_empty_tools_yields_no_fingerprint() -> None:
    assert _compute_tools_fingerprint([]) is None
    assert _compute_tools_fingerprint(None) is None


def test_tools_fingerprint_on_run_state_is_shared_across_nodes() -> None:
    """Every LLM call in the run carries the same tools fingerprint
    on its NeutralCall metadata (the adapter snapshots at compile
    time, not per node)."""
    from inkfoot._instrument import _STORAGE

    g = _StubGraph(
        tools=[{"name": "search", "description": "x"}, {"name": "calc"}]
    )
    inst = instrument(g, task="t")
    g.invoke({"input": 1})

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call'"
    )
    metas = [
        json.loads(r["payload_json"])["metadata"] for r in cur.fetchall()
    ]
    fingerprints = {m.get("tools_fingerprint") for m in metas}
    # One value, shared across nodes.
    assert fingerprints == {inst.tools_fingerprint}
    assert inst.tools_fingerprint is not None


# ----------------------------------------------------------------------
# Capability surface
# ----------------------------------------------------------------------


def test_adapter_registers_active_on_instrument() -> None:
    """Pattern-A policies must still register cleanly when LangGraph
    is active (the adapter's empty ``supported_policies`` plus the
    pattern-fallback path in ``register_policies`` lets them
    through)."""
    g = _StubGraph()
    instrument(g, task="t")
    assert AdapterRegistry.get_active() is not None
    assert AdapterRegistry.get_active().name == "langgraph"


def test_supported_policies_default_is_empty_set() -> None:
    assert LangGraphAdapter().supported_policies() == set()


# ----------------------------------------------------------------------
# Shutdown reverses the patches
# ----------------------------------------------------------------------


def test_shutdown_restores_original_entry_points_and_nodes() -> None:
    g = _StubGraph()
    original_invoke = g.invoke
    original_retrieve = g.nodes["retrieve"]
    inst = instrument(g, task="t")

    # After instrument, the bound invoke is wrapped (different
    # function object).
    assert g.invoke is not original_invoke

    inst.shutdown()

    # After shutdown, the original is restored (instance-dict entry
    # was deleted, class-level method is back).
    assert g.invoke == original_invoke or g.invoke is original_invoke
    assert g.nodes["retrieve"] is original_retrieve


def test_shutdown_is_idempotent() -> None:
    g = _StubGraph()
    inst = instrument(g, task="t")
    inst.shutdown()
    inst.shutdown()  # second call no-op


def test_shutdown_clears_active_adapter_via_LangGraphAdapter_shutdown() -> None:
    g = _StubGraph()
    instrument(g, task="t")
    assert AdapterRegistry.get_active() is not None
    LangGraphAdapter().shutdown()
    assert AdapterRegistry.get_active() is None


def test_instrumentation_shutdown_auto_deactivates_when_last_handle_closes() -> None:
    """review finding #4: a user calling ``inst.shutdown()``
    on the only live handle should leave the adapter registry's
    active pointer clean. Previously the pointer lingered until
    ``LangGraphAdapter.shutdown()`` was called explicitly."""
    g = _StubGraph()
    inst = instrument(g, task="t")
    assert AdapterRegistry.get_active() is not None

    inst.shutdown()
    assert AdapterRegistry.get_active() is None


def test_instrumentation_shutdown_keeps_adapter_active_while_other_handles_live() -> None:
    """Two graphs instrumented → shutting down one leaves the other
    + the registry pointer intact. Auto-deactivation only fires when
    the install count hits zero."""
    g1 = _StubGraph()
    g2 = _StubGraph()
    inst1 = instrument(g1, task="t1")
    inst2 = instrument(g2, task="t2")
    assert AdapterRegistry.get_active() is not None

    inst1.shutdown()
    # The second instrumentation still live, so the registry pointer
    # MUST stay set.
    assert AdapterRegistry.get_active() is not None

    inst2.shutdown()
    # Now the last handle is gone — auto-deactivate fires.
    assert AdapterRegistry.get_active() is None
