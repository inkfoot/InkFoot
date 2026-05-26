"""E1-S3 — OpenAI Agents SDK adapter unit tests.

Duck-typed stub mimics the Agent class surface:

  * ``run(prompt)`` / ``run_async(prompt)`` — the entry points the
    adapter wraps to scope an :func:`agent_run`.
  * ``_call_tool(tool_name, tool_args)`` — the tool-dispatch method
    the adapter intercepts to emit ``tool_dispatched`` events.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.openai_agents import (
    OpenAIAgentsAdapter,
    _stable_args_hash,
    instrument,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path) -> Any:
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.storage.sqlite import SQLiteStorage

    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    shutdown()


class _StubAgent:
    """Mimics a thin slice of the OpenAI Agents SDK Agent class."""

    def __init__(self) -> None:
        self.run_calls: list[Any] = []
        self.tools_called: list[tuple[str, Any]] = []

    def run(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        # Simulate the loop calling a tool partway through.
        self._call_tool("search", {"q": "weather", "city": "Tokyo"})
        return {"output": "answer-to-" + prompt}

    async def run_async(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        self._call_tool("search", {"q": "weather", "city": "Tokyo"})
        return {"output": "async-" + prompt}

    def _call_tool(self, tool_name: str, tool_args: Any) -> Any:
        self.tools_called.append((tool_name, tool_args))
        return {"result": "ok"}


# ----------------------------------------------------------------------
# Entry-point wrapping
# ----------------------------------------------------------------------


def test_run_is_scoped_under_an_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="oa-test")
    agent.run("hi")

    rows = list(_STORAGE._conn().execute("SELECT task, status FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "oa-test"
    assert rows[0]["status"] == "complete"


def test_run_async_is_scoped_under_an_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="oa-async")
    asyncio.run(agent.run_async("hi"))

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "oa-async"


def test_instrument_is_idempotent() -> None:
    agent = _StubAgent()
    h1 = instrument(agent, task="t")
    h2 = instrument(agent, task="t")
    assert h1 is h2


# ----------------------------------------------------------------------
# Tool dispatch events
# ----------------------------------------------------------------------


def test_tool_dispatch_emits_event_with_name_hash_latency() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="t")
    agent.run("hi")

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='tool_dispatched'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["tool_name"] == "search"
    assert isinstance(payload["tool_args_hash"], str)
    assert len(payload["tool_args_hash"]) == 16
    assert isinstance(payload["dispatch_latency_ms"], int)
    assert payload["dispatch_latency_ms"] >= 0


def test_stable_args_hash_is_deterministic() -> None:
    h1 = _stable_args_hash({"q": "weather", "city": "Tokyo"})
    h2 = _stable_args_hash({"city": "Tokyo", "q": "weather"})  # key order
    assert h1 == h2  # sort_keys keeps the hash stable
    assert len(h1) == 16


def test_stable_args_hash_falls_back_to_repr_for_non_json_args() -> None:
    h = _stable_args_hash({"fn": lambda: None})  # callables don't JSON
    assert isinstance(h, str) and len(h) == 16


def test_tool_dispatch_outside_an_active_run_does_not_emit() -> None:
    """Calling the wrapped tool dispatch directly (without run() in
    between) leaves no event row — the event needs an active run id
    to attach to."""
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="t")
    agent._call_tool("search", {"q": "x"})

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='tool_dispatched'"
    )
    assert cur.fetchone()["n"] == 0


# ----------------------------------------------------------------------
# Capability declaration + registry
# ----------------------------------------------------------------------


def test_adapter_activates_on_instrument() -> None:
    agent = _StubAgent()
    instrument(agent, task="t")
    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == "openai_agents"


def test_supported_policies_default_is_empty_set() -> None:
    assert OpenAIAgentsAdapter().supported_policies() == set()


def test_shutdown_restores_originals_and_is_idempotent() -> None:
    agent = _StubAgent()
    original_run = agent.run
    original_dispatch = agent._call_tool
    inst = instrument(agent, task="t")

    assert agent.run is not original_run

    inst.shutdown()
    assert agent.run is original_run or agent.run == original_run
    assert agent._call_tool is original_dispatch or agent._call_tool == original_dispatch
    inst.shutdown()  # idempotent
