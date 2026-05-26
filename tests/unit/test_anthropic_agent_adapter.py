"""E1-S4 — Anthropic Agent SDK adapter unit tests.

The adapter is structurally identical to the OpenAI Agents adapter
(shared helpers in :mod:`inkfoot.adapters.openai_agents`), so the
test surface mirrors that suite but keeps the assertions tight on
the Anthropic-named pieces (adapter name, instrumentation marker,
top-level re-export).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.anthropic_agent import (
    AnthropicAgentAdapter,
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


class _StubAnthAgent:
    def __init__(self) -> None:
        self.tools_called: list[tuple[str, Any]] = []

    def run(self, prompt: str) -> dict[str, Any]:
        self._call_tool("search", {"q": prompt})
        return {"output": prompt[::-1]}

    async def run_async(self, prompt: str) -> dict[str, Any]:
        self._call_tool("search", {"q": prompt})
        return {"output": "async:" + prompt}

    def _call_tool(self, tool_name: str, tool_args: Any) -> Any:
        self.tools_called.append((tool_name, tool_args))
        return {"result": "ok"}


def test_run_is_scoped_under_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAnthAgent()
    instrument(agent, task="anth-test")
    agent.run("hi")

    rows = list(_STORAGE._conn().execute("SELECT task, status FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "anth-test"
    assert rows[0]["status"] == "complete"


def test_run_async_is_scoped() -> None:
    agent = _StubAnthAgent()
    instrument(agent, task="anth-async")
    asyncio.run(agent.run_async("hi"))

    from inkfoot._instrument import _STORAGE

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "anth-async"


def test_tool_dispatch_emits_event() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAnthAgent()
    instrument(agent, task="t")
    agent.run("hi")

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='tool_dispatched'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["tool_name"] == "search"
    assert isinstance(payload["dispatch_latency_ms"], int)


def test_adapter_activates_with_correct_name() -> None:
    agent = _StubAnthAgent()
    instrument(agent, task="t")
    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == "anthropic_agent"


def test_instrument_is_idempotent() -> None:
    agent = _StubAnthAgent()
    h1 = instrument(agent, task="t")
    h2 = instrument(agent, task="t")
    assert h1 is h2


def test_supported_policies_default_is_empty_set() -> None:
    assert AnthropicAgentAdapter().supported_policies() == set()


def test_shutdown_restores_originals() -> None:
    agent = _StubAnthAgent()
    original_run = agent.run
    original_dispatch = agent._call_tool
    inst = instrument(agent, task="t")

    assert agent.run is not original_run

    inst.shutdown()
    assert agent.run is original_run or agent.run == original_run
    assert (
        agent._call_tool is original_dispatch
        or agent._call_tool == original_dispatch
    )


def test_top_level_module_reexports_instrument() -> None:
    import inkfoot.anthropic_agent as anth_pkg

    assert anth_pkg.instrument is instrument
