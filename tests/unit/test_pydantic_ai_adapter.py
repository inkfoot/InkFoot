"""Pydantic AI adapter unit tests.

Duck-typed stubs mimic the Agent class surface:

  * ``run(prompt)`` (async) / ``run_sync(prompt)`` (sync) — the
    entry points the adapter wraps to scope an :func:`agent_run`.
  * ``_function_tools`` — the registered-tool registry (``name →
    Tool``) whose ``Tool.run`` the adapter wraps to emit
    ``tool_dispatched`` events. A second stub shape keeps the
    registry on a toolset object (``_function_toolset.tools``) the
    way newer SDK builds do.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.pydantic_ai import (
    PydanticAIAdapter,
    instrument,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path) -> Any:
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.adapters.pydantic_ai import _default_adapter
    from inkfoot.storage.sqlite import SQLiteStorage

    _default_adapter._install_count = 0
    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    _default_adapter._install_count = 0
    shutdown()


class _StubToolCallPart:
    """Mimics pydantic-ai's ToolCallPart — the first positional arg
    ``Tool.run`` receives, carrying ``tool_name`` / ``args``."""

    def __init__(self, tool_name: str, args: Any) -> None:
        self.tool_name = tool_name
        self.args = args


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[Any] = []

    def run(self, part: Any, ctx: Any = None) -> Any:
        self.calls.append(part)
        return {"result": "ok"}


class _StubAgent:
    """Mimics a thin slice of the Pydantic AI Agent class — dict
    registry shape (``_function_tools``)."""

    def __init__(self) -> None:
        self.run_calls: list[Any] = []
        self.tool = _StubTool("get_weather")
        self._function_tools = {"get_weather": self.tool}

    def _dispatch(self) -> None:
        self.tool.run(
            _StubToolCallPart("get_weather", {"city": "Tokyo"})
        )

    async def run(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        self._dispatch()
        return {"output": "async-" + prompt}

    def run_sync(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        self._dispatch()
        return {"output": "sync-" + prompt}


class _ToolsetStubAgent(_StubAgent):
    """Same surface, but the registry lives on a toolset object
    (``_function_toolset.tools``) the way newer SDK builds keep it."""

    def __init__(self) -> None:
        super().__init__()
        del self._function_tools
        self._function_toolset = SimpleNamespace(
            tools={"get_weather": self.tool}
        )


class _BareStubAgent:
    """Entry points only — no tool registry, no dispatch method. The
    adapter must still scope runs and simply emit no tool events."""

    def __init__(self) -> None:
        self.run_calls: list[Any] = []

    def run_sync(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        return {"output": "sync-" + prompt}


class _DispatchAndRegistryStubAgent(_StubAgent):
    """Both dispatch layers present: a generic dispatch method
    (``_call_tool``) that routes through the registered tool's
    ``run``. Wrapping both layers would emit two events per call —
    the adapter must wrap the registry only."""

    def _call_tool(self, part: Any) -> Any:
        return self.tool.run(part)

    def _dispatch(self) -> None:
        self._call_tool(_StubToolCallPart("get_weather", {"city": "Tokyo"}))


class _DispatchOnlyStubAgent:
    """Generic dispatch method but no tool registry — the fallback
    layer the adapter probes when no registry is found."""

    def __init__(self) -> None:
        self.run_calls: list[Any] = []

    def _call_tool(self, part: Any) -> Any:
        return {"result": "ok"}

    def run_sync(self, prompt: str) -> dict[str, Any]:
        self.run_calls.append(prompt)
        self._call_tool(_StubToolCallPart("get_weather", {"city": "Tokyo"}))
        return {"output": "sync-" + prompt}


# ----------------------------------------------------------------------
# Entry-point wrapping
# ----------------------------------------------------------------------


def test_run_sync_is_scoped_under_an_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="pai-test")
    agent.run_sync("hi")

    rows = list(_STORAGE._conn().execute("SELECT task, status FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "pai-test"
    assert rows[0]["status"] == "complete"


def test_async_run_is_scoped_under_an_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="pai-async")
    asyncio.run(agent.run("hi"))

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "pai-async"


def test_default_task_label_is_pydantic_ai() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent)
    agent.run_sync("hi")

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert rows[0]["task"] == "pydantic_ai"


def test_run_inside_existing_agent_run_does_not_open_second_run() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="inner")
    with inkfoot.agent_run(task="outer"):
        agent.run_sync("hi")

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "outer"


def test_instrument_is_idempotent() -> None:
    agent = _StubAgent()
    h1 = instrument(agent, task="t")
    h2 = instrument(agent, task="t")
    assert h1 is h2


# ----------------------------------------------------------------------
# Registered-tool dispatch events
# ----------------------------------------------------------------------


def test_registered_tool_run_emits_tool_dispatched_event() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="t")
    agent.run_sync("hi")

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='tool_dispatched'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["tool_name"] == "get_weather"
    assert isinstance(payload["tool_args_hash"], str)
    assert len(payload["tool_args_hash"]) == 16
    assert payload["dispatch_latency_ms"] >= 0


def test_toolset_shaped_registry_is_wrapped_too() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _ToolsetStubAgent()
    instrument(agent, task="t")
    agent.run_sync("hi")

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='tool_dispatched'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["tool_name"] == "get_weather"


def test_agent_without_tool_registry_still_scopes_runs() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _BareStubAgent()
    instrument(agent, task="bare")
    agent.run_sync("hi")

    runs = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(runs) == 1
    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='tool_dispatched'"
    )
    assert cur.fetchone()["n"] == 0


def test_tool_run_outside_an_active_run_does_not_emit() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent()
    instrument(agent, task="t")
    agent.tool.run(_StubToolCallPart("get_weather", {"city": "Oslo"}))

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='tool_dispatched'"
    )
    assert cur.fetchone()["n"] == 0


def test_registry_wins_over_generic_dispatch_method_no_double_emit() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _DispatchAndRegistryStubAgent()
    instrument(agent, task="t")
    agent.run_sync("hi")

    # The generic probe was skipped: no instance-level wrap installed.
    assert "_call_tool" not in agent.__dict__
    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='tool_dispatched'"
    )
    assert cur.fetchone()["n"] == 1


def test_generic_dispatch_probe_still_fires_without_a_registry() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _DispatchOnlyStubAgent()
    instrument(agent, task="t")
    agent.run_sync("hi")

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='tool_dispatched'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["tool_name"] == "get_weather"


# ----------------------------------------------------------------------
# Capability declaration + registry
# ----------------------------------------------------------------------


def test_adapter_activates_on_instrument() -> None:
    agent = _StubAgent()
    instrument(agent, task="t")
    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == "pydantic_ai"


def test_supported_policies_enumerates_modification_policies() -> None:
    from inkfoot.policy import CheapSummariser, LazyToolExposure

    assert PydanticAIAdapter().supported_policies() == {
        LazyToolExposure,
        CheapSummariser,
    }


def test_shutdown_restores_originals_and_is_idempotent() -> None:
    agent = _StubAgent()
    original_run_sync = agent.run_sync
    original_tool_run = agent.tool.run
    inst = instrument(agent, task="t")

    assert agent.run_sync is not original_run_sync
    assert agent.tool.run is not original_tool_run

    inst.shutdown()
    assert "run_sync" not in agent.__dict__
    assert "run" not in agent.tool.__dict__
    inst.shutdown()  # idempotent


def test_instrumentation_shutdown_auto_deactivates_when_last_handle_closes() -> None:
    agent = _StubAgent()
    inst = instrument(agent, task="t")
    assert AdapterRegistry.get_active() is not None

    inst.shutdown()
    assert AdapterRegistry.get_active() is None


def test_two_instrumented_agents_keep_adapter_active_until_both_shutdown() -> None:
    a1 = _StubAgent()
    a2 = _StubAgent()
    inst1 = instrument(a1, task="t1")
    inst2 = instrument(a2, task="t2")
    assert AdapterRegistry.get_active() is not None

    inst1.shutdown()
    assert AdapterRegistry.get_active() is not None

    inst2.shutdown()
    assert AdapterRegistry.get_active() is None
