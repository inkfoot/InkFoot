"""LangGraph adapter cross-version validation.

The adapter is duck-typed, so these tests exercise it against two
*stub* graph shapes that mirror how the node registry and entry
points are laid out in the two supported LangGraph lines:

* **legacy** — nodes are bare callables in a plain ``dict`` on
  ``graph.nodes`` (the 0.2/0.3 shape).
* **modern** — nodes are wrapper objects exposing a ``func`` / ``afunc``
  sync/async pair, held in a custom ``MutableMapping`` reached through
  ``graph.graph.nodes`` (the reshuffled 1.x shape).

Both are parametrised through the same assertions so a regression in
either layout fails loudly. The streaming entry points (``stream`` /
``astream``) additionally pin the run-scoping fix: their nodes only
execute as the caller pulls items, so the run must stay open across
the whole iteration — otherwise node events leak into a stray ambient
run and we'd see *two* runs instead of one.

When a real ``langgraph`` is installed (the CI version matrix), a
final leg compiles an actual graph and checks the adapter wraps it
without raising.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.langgraph import LangGraphAdapter, instrument


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path):
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.adapters.langgraph import _default_adapter
    from inkfoot.storage.sqlite import SQLiteStorage

    _default_adapter._install_count = 0
    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], langchain=False, storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    _default_adapter._install_count = 0
    shutdown()


# ----------------------------------------------------------------------
# Shared node body
# ----------------------------------------------------------------------


def _emit_fake_llm_call() -> None:
    """Emit a real ``llm_call`` event tagged with the active node so
    tests can assert per-node attribution flowed through the scope."""
    from dataclasses import asdict as _asdict

    from ulid import ULID

    from inkfoot._instrument import _STORAGE
    from inkfoot._run_context import current_run_id, get_or_create_run_state
    from inkfoot.normalise.openai import OpenAITranslator
    from inkfoot.shims._emit import _next_sequence

    run_id = current_run_id()
    assert run_id is not None, "node ran outside an active run"
    state = get_or_create_run_state(run_id)
    call = OpenAITranslator().translate(
        request={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        response={"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    _STORAGE.insert_event(
        event_id=str(ULID()),
        run_id=run_id,
        kind="llm_call",
        occurred_at=1,
        sequence=_next_sequence(run_id),
        payload_json=json.dumps(_asdict(call), default=str),
        capture_mode="metadata",
    )


# ----------------------------------------------------------------------
# Legacy (0.2/0.3) stub: bare callables in a plain dict on ``.nodes``
# ----------------------------------------------------------------------


class _LegacyGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Any] = {
            "retrieve": self._make_node("retrieve"),
            "synthesise": self._make_node("synthesise"),
        }

    @staticmethod
    def _make_node(_name: str):
        def _node(state: dict) -> dict:
            _emit_fake_llm_call()
            return state

        return _node

    def invoke(self, state: dict) -> dict:
        for fn in list(self.nodes.values()):
            state = fn(state)
        return state

    async def ainvoke(self, state: dict) -> dict:
        for fn in list(self.nodes.values()):
            state = fn(state)
        return state

    def stream(self, state: dict):
        for fn in list(self.nodes.values()):
            state = fn(state)
            yield state

    async def astream(self, state: dict):
        for fn in list(self.nodes.values()):
            state = fn(state)
            yield state


# ----------------------------------------------------------------------
# Modern (1.x) stub: wrapper objects with func/afunc in a custom
# MutableMapping reached through ``.graph.nodes``
# ----------------------------------------------------------------------


class _NodeMap(MutableMapping):
    """A non-``dict`` mutable mapping — mimics 1.x swapping the plain
    dict for a custom node registry type."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v

    def __delitem__(self, k):
        del self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)


class _NodeSpec:
    """A node wrapper exposing the RunnableCallable-style sync/async
    pair the adapter wraps in place."""

    def __init__(self, name: str) -> None:
        self.name = name

        def _sync(state: dict) -> dict:
            _emit_fake_llm_call()
            return state

        async def _async(state: dict) -> dict:
            _emit_fake_llm_call()
            return state

        self.func = _sync
        self.afunc = _async


class _ModernGraph:
    def __init__(self) -> None:
        node_map = _NodeMap()
        node_map["retrieve"] = _NodeSpec("retrieve")
        node_map["synthesise"] = _NodeSpec("synthesise")
        # No top-level ``.nodes``: nodes are reached via ``.graph.nodes``.
        self.graph = SimpleNamespace(nodes=node_map)

    def _specs(self):
        return list(self.graph.nodes.values())

    def invoke(self, state: dict) -> dict:
        for spec in self._specs():
            state = spec.func(state)
        return state

    async def ainvoke(self, state: dict) -> dict:
        for spec in self._specs():
            state = await spec.afunc(state)
        return state

    def stream(self, state: dict):
        for spec in self._specs():
            state = spec.func(state)
            yield state

    async def astream(self, state: dict):
        for spec in self._specs():
            state = await spec.afunc(state)
            yield state


_GRAPH_FACTORIES = {
    "legacy": _LegacyGraph,
    "modern": _ModernGraph,
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _runs(storage) -> list[dict]:
    return [dict(r) for r in storage._conn().execute("SELECT * FROM runs")]


def _events(storage, kind: str) -> list[dict]:
    cur = storage._conn().execute(
        "SELECT payload_json FROM events WHERE kind = ? ORDER BY sequence", (kind,)
    )
    return [json.loads(r["payload_json"]) for r in cur.fetchall()]


def _drain(result) -> None:
    """Consume a sync iterator if the entry point returned one."""
    for _ in result:
        pass


async def _adrain(agen) -> None:
    async for _ in agen:
        pass


# ----------------------------------------------------------------------
# Tests — parametrised over both layouts
# ----------------------------------------------------------------------


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_sync_invoke_scopes_one_run_with_node_events(_isolated_state, layout) -> None:
    from inkfoot._instrument import _STORAGE

    graph = _GRAPH_FACTORIES[layout]()
    instrument(graph, task=f"{layout}-invoke")
    graph.invoke({"input": 1})

    runs = _runs(_STORAGE)
    assert len(runs) == 1
    assert runs[0]["task"] == f"{layout}-invoke"
    # 2 nodes × (enter + exit).
    assert len(_events(_STORAGE, "node_enter")) == 2
    assert len(_events(_STORAGE, "node_exit")) == 2


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_async_invoke_scopes_one_run(_isolated_state, layout) -> None:
    from inkfoot._instrument import _STORAGE

    graph = _GRAPH_FACTORIES[layout]()
    instrument(graph, task=f"{layout}-ainvoke")
    asyncio.run(graph.ainvoke({"input": 1}))

    assert len(_runs(_STORAGE)) == 1
    assert len(_events(_STORAGE, "node_enter")) == 2


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_node_name_flows_onto_llm_call_metadata(_isolated_state, layout) -> None:
    from inkfoot._instrument import _STORAGE

    graph = _GRAPH_FACTORIES[layout]()
    instrument(graph, task="t")
    graph.invoke({"input": 1})

    metas = [p["metadata"] for p in _events(_STORAGE, "llm_call")]
    node_names = [m.get("node_name") for m in metas]
    assert node_names == ["retrieve", "synthesise"]


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_sync_stream_keeps_run_open_across_iteration(_isolated_state, layout) -> None:
    """If streaming scope regressed, node events would land in a stray
    ambient run and we'd see two runs. Exactly one proves the run
    stayed open across the whole iteration."""
    from inkfoot._instrument import _STORAGE

    graph = _GRAPH_FACTORIES[layout]()
    instrument(graph, task=f"{layout}-stream")
    _drain(graph.stream({"input": 1}))

    runs = _runs(_STORAGE)
    assert len(runs) == 1
    assert runs[0]["task"] == f"{layout}-stream"
    assert len(_events(_STORAGE, "node_enter")) == 2


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_async_stream_keeps_run_open_across_iteration(_isolated_state, layout) -> None:
    from inkfoot._instrument import _STORAGE

    graph = _GRAPH_FACTORIES[layout]()
    instrument(graph, task=f"{layout}-astream")
    asyncio.run(_adrain(graph.astream({"input": 1})))

    runs = _runs(_STORAGE)
    assert len(runs) == 1
    assert len(_events(_STORAGE, "node_enter")) == 2


def _node_callables(graph: Any) -> list[Any]:
    """The callables the adapter wraps, for either layout."""
    if isinstance(graph, _LegacyGraph):
        return list(graph.nodes.values())
    return [spec.func for spec in graph.graph.nodes.values()] + [
        spec.afunc for spec in graph.graph.nodes.values()
    ]


@pytest.mark.parametrize("layout", list(_GRAPH_FACTORIES))
def test_instrument_wraps_then_shutdown_restores_nodes(
    _isolated_state, layout
) -> None:
    graph = _GRAPH_FACTORIES[layout]()
    inst = instrument(graph, task="t")
    # After instrument, every wrapped node carries the wrap marker.
    assert all(
        hasattr(fn, "__inkfoot_wrapped_node__") for fn in _node_callables(graph)
    )

    inst.shutdown()
    # After shutdown, the originals are back — no marker left behind.
    assert not any(
        hasattr(fn, "__inkfoot_wrapped_node__") for fn in _node_callables(graph)
    )


# ----------------------------------------------------------------------
# Real LangGraph leg — runs only where the package is installed. The
# CI `langgraph-matrix` job pins 0.3.x and 1.0.x and runs this file, so
# these legs are the actual cross-version validation; the stubs above
# are the fast offline guard.
# ----------------------------------------------------------------------


def _build_real_graph():
    """Compile a one-node graph against whatever LangGraph is installed.
    Skips (not fails) if the graph-construction API has shifted in a way
    this helper doesn't speak."""
    pytest.importorskip("langgraph", reason="langgraph not installed")
    try:
        from typing import TypedDict

        from langgraph.graph import END, StateGraph
    except Exception as exc:  # pragma: no cover - version-dependent import
        pytest.skip(f"langgraph graph API unavailable: {exc}")

    class _State(TypedDict, total=False):
        steps: int

    def _node(state: _State) -> dict:
        return {"steps": state.get("steps", 0) + 1}

    builder = StateGraph(_State)
    builder.add_node("step", _node)
    builder.set_entry_point("step")
    builder.add_edge("step", END)
    return builder.compile()


def test_real_langgraph_invoke_scopes_run_and_wraps_nodes(_isolated_state) -> None:
    from inkfoot._instrument import _STORAGE

    compiled = _build_real_graph()
    inst = instrument(compiled, task="real-langgraph")
    try:
        result = compiled.invoke({"steps": 0})
        assert result["steps"] == 1
        # A run was scoped around the graph execution …
        tasks = [r["task"] for r in _runs(_STORAGE)]
        assert "real-langgraph" in tasks
        # … and node wrapping actually fired on the real compiled graph
        # (this is the regression that a stub-only test can't catch).
        assert len(_events(_STORAGE, "node_enter")) >= 1
        assert AdapterRegistry.get_active() is not None
    finally:
        inst.shutdown()


def test_real_langgraph_astream_keeps_run_open(_isolated_state) -> None:
    """``astream`` is the entry point 1.x reworked into an async
    generator. Drive it on the real graph and confirm the run stays
    scoped across the streamed iteration with node events landing."""
    from inkfoot._instrument import _STORAGE

    compiled = _build_real_graph()
    inst = instrument(compiled, task="real-astream")
    try:
        async def _drive() -> int:
            seen = 0
            async for _chunk in compiled.astream({"steps": 0}):
                seen += 1
            return seen

        chunks = asyncio.run(_drive())
        assert chunks >= 1
        tasks = [r["task"] for r in _runs(_STORAGE)]
        assert "real-astream" in tasks
        assert len(_events(_STORAGE, "node_enter")) >= 1
    finally:
        inst.shutdown()
