"""Cross-adapter contract harness.

Every framework adapter — whatever SDK it wraps — must honour the
same behavioural contract, pinned here once and parametrised over all
of them:

  1. ``instrument(target)`` installs without the SDK present
     (adapters are duck-typed; stubs stand in for real objects).
  2. The wrapped sync entry point scopes a run: one ``runs`` row,
     default task label = adapter name, status ``complete``.
  3. A caller-supplied ``task=`` label wins over the default.
  4. Re-entrancy: calling the entry point inside an existing
     ``inkfoot.agent_run`` opens no second run.
  5. ``instrument`` is idempotent — same handle back.
  6. ``shutdown()`` restores the entry point and is itself
     idempotent; the registry's active pointer clears with the last
     live handle.
  7. The capability surface (``supported_policies()``) matches the
     published matrix.
  8. The top-level convenience module (``inkfoot.<name>``) re-exports
     the adapter class and ``instrument``.

When a new framework adapter lands, add one ``AdapterSpec`` row and
the whole contract applies to it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.policy import CheapSummariser, LazyToolExposure


@dataclass(frozen=True)
class AdapterSpec:
    """One adapter's contract parameters."""

    name: str  # AdapterRegistry name, also the default task label
    adapter_module: str  # inkfoot.adapters.<module>
    adapter_cls: str
    top_module: str  # inkfoot.<module> convenience re-export
    sync_entry: str  # sync entry-point method the adapter wraps
    observation_only: bool  # True → supported_policies() == set()


SPECS = [
    AdapterSpec(
        name="langgraph",
        adapter_module="inkfoot.adapters.langgraph",
        adapter_cls="LangGraphAdapter",
        top_module="inkfoot.langgraph",
        sync_entry="invoke",
        observation_only=False,
    ),
    AdapterSpec(
        name="openai_agents",
        adapter_module="inkfoot.adapters.openai_agents",
        adapter_cls="OpenAIAgentsAdapter",
        top_module="inkfoot.openai_agents",
        sync_entry="run",
        observation_only=False,
    ),
    AdapterSpec(
        name="anthropic_agent",
        adapter_module="inkfoot.adapters.anthropic_agent",
        adapter_cls="AnthropicAgentAdapter",
        top_module="inkfoot.anthropic_agent",
        sync_entry="run",
        observation_only=False,
    ),
    AdapterSpec(
        name="pydantic_ai",
        adapter_module="inkfoot.adapters.pydantic_ai",
        adapter_cls="PydanticAIAdapter",
        top_module="inkfoot.pydantic_ai",
        sync_entry="run_sync",
        observation_only=False,
    ),
    AdapterSpec(
        name="crewai",
        adapter_module="inkfoot.adapters.crewai",
        adapter_cls="CrewAIAdapter",
        top_module="inkfoot.crewai",
        sync_entry="kickoff",
        observation_only=True,
    ),
]

PARAMS = [pytest.param(spec, id=spec.name) for spec in SPECS]


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


def _adapter(spec: AdapterSpec) -> Any:
    module = importlib.import_module(spec.adapter_module)
    return getattr(module, spec.adapter_cls)()


def _make_stub(entry_name: str) -> Any:
    """Duck-typed target exposing exactly one sync entry point.

    Adapters must tolerate everything else being absent (no tool
    registry, no agents/tasks lists, no async twin)."""

    class _Stub:
        def __init__(self) -> None:
            self.calls: list[Any] = []

    def entry(self: Any, payload: Any = None) -> dict[str, Any]:
        self.calls.append(payload)
        return {"output": payload}

    setattr(_Stub, entry_name, entry)
    return _Stub()


def _run_rows() -> list[Any]:
    from inkfoot._instrument import _STORAGE

    return list(
        _STORAGE._conn().execute(  # type: ignore[union-attr]
            "SELECT task, status FROM runs"
        )
    )


@pytest.mark.parametrize("spec", PARAMS)
def test_entry_point_scopes_run_with_default_task_label(
    spec: AdapterSpec,
) -> None:
    stub = _make_stub(spec.sync_entry)
    _adapter(spec).instrument(stub)
    getattr(stub, spec.sync_entry)("hi")

    rows = _run_rows()
    assert len(rows) == 1
    assert rows[0]["task"] == spec.name
    assert rows[0]["status"] == "complete"
    assert stub.calls == ["hi"]


@pytest.mark.parametrize("spec", PARAMS)
def test_caller_task_label_overrides_default(spec: AdapterSpec) -> None:
    stub = _make_stub(spec.sync_entry)
    _adapter(spec).instrument(stub, task="contract-task")
    getattr(stub, spec.sync_entry)("hi")

    rows = _run_rows()
    assert len(rows) == 1
    assert rows[0]["task"] == "contract-task"


@pytest.mark.parametrize("spec", PARAMS)
def test_entry_point_is_reentrant_under_outer_run(
    spec: AdapterSpec,
) -> None:
    stub = _make_stub(spec.sync_entry)
    _adapter(spec).instrument(stub, task="inner")
    with inkfoot.agent_run(task="outer"):
        getattr(stub, spec.sync_entry)("hi")

    rows = _run_rows()
    assert len(rows) == 1
    assert rows[0]["task"] == "outer"


@pytest.mark.parametrize("spec", PARAMS)
def test_instrument_is_idempotent(spec: AdapterSpec) -> None:
    stub = _make_stub(spec.sync_entry)
    adapter = _adapter(spec)
    h1 = adapter.instrument(stub)
    h2 = adapter.instrument(stub)
    assert h1 is h2


@pytest.mark.parametrize("spec", PARAMS)
def test_shutdown_restores_entry_point_and_is_idempotent(
    spec: AdapterSpec,
) -> None:
    stub = _make_stub(spec.sync_entry)
    class_method = getattr(type(stub), spec.sync_entry)
    inst = _adapter(spec).instrument(stub)
    assert spec.sync_entry in stub.__dict__  # wrapped shadow installed

    inst.shutdown()
    assert spec.sync_entry not in stub.__dict__
    assert getattr(type(stub), spec.sync_entry) is class_method
    inst.shutdown()  # second call must be a no-op


@pytest.mark.parametrize("spec", PARAMS)
def test_registry_activates_and_clears_with_last_handle(
    spec: AdapterSpec,
) -> None:
    adapter = _adapter(spec)
    s1 = _make_stub(spec.sync_entry)
    s2 = _make_stub(spec.sync_entry)
    i1 = adapter.instrument(s1)
    i2 = adapter.instrument(s2)

    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == spec.name

    i1.shutdown()
    assert AdapterRegistry.get_active() is not None
    i2.shutdown()
    assert AdapterRegistry.get_active() is None


@pytest.mark.parametrize("spec", PARAMS)
def test_capability_surface_matches_published_matrix(
    spec: AdapterSpec,
) -> None:
    policies = _adapter(spec).supported_policies()
    if spec.observation_only:
        assert policies == set()
    else:
        assert policies == {LazyToolExposure, CheapSummariser}


@pytest.mark.parametrize("spec", PARAMS)
def test_detect_returns_bool_without_sdk_installed(
    spec: AdapterSpec,
) -> None:
    assert isinstance(_adapter(spec).detect(), bool)


@pytest.mark.parametrize("spec", PARAMS)
def test_top_level_module_reexports_adapter_and_instrument(
    spec: AdapterSpec,
) -> None:
    top = importlib.import_module(spec.top_module)
    assert callable(getattr(top, "instrument"))
    assert getattr(top, spec.adapter_cls) is getattr(
        importlib.import_module(spec.adapter_module), spec.adapter_cls
    )
    assert spec.adapter_cls in top.__all__
    assert "instrument" in top.__all__
