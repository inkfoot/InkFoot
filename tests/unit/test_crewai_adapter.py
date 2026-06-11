"""CrewAI adapter unit tests.

Duck-typed stubs mimic the Crew / Agent / Task surfaces:

  * ``Crew.kickoff`` / ``Crew.kickoff_async`` — the entry points the
    adapter wraps to scope an :func:`agent_run`.
  * ``Agent.execute_task`` / ``Task.execute_sync`` — the per-member
    execute methods the adapter wraps so LLM calls inside carry
    ``metadata["agent_name"]`` / ``metadata["task_name"]``.

A second stub family rejects unknown public attribute assignment the
way pydantic models do (real CrewAI objects are pydantic models), to
pin the adapter's ``object.__setattr__`` install fallback.

The fake ``llm_call`` events are produced by driving the OpenAI
translator directly (no SDK needed) — the same recipe the LangGraph
adapter tests use — so the attribution asserted here is the real
translator-stamped metadata, not a hand-built payload.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import inkfoot
from inkfoot.adapters._registry import AdapterRegistry
from inkfoot.adapters.crewai import (
    CrewAIAdapter,
    instrument,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path) -> Any:
    from inkfoot._instrument import shutdown
    from inkfoot._run_context import _clear_current_run
    from inkfoot.adapters.crewai import _default_adapter
    from inkfoot.storage.sqlite import SQLiteStorage

    _default_adapter._install_count = 0
    db_path = tmp_path / "runs.db"
    inkfoot.instrument(sdks=[], storage=SQLiteStorage(path=db_path))
    yield db_path
    _clear_current_run()
    AdapterRegistry.clear()
    _default_adapter._install_count = 0
    shutdown()


def _emit_fake_llm_call() -> None:
    """Write one ``llm_call`` event through the OpenAI translator so
    the event carries whatever metadata the run state holds at call
    time."""
    from ulid import ULID

    from inkfoot._instrument import _STORAGE
    from inkfoot._run_context import current_run_id, get_or_create_run_state
    from inkfoot.normalise.openai import OpenAITranslator
    from inkfoot.shims._emit import _next_sequence

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


class _StubAgent:
    def __init__(self, role: str, name: Optional[str] = None) -> None:
        self.role = role
        if name is not None:
            self.name = name
        self.executed: list[Any] = []

    def execute_task(
        self, task: Any = None, context: Any = None, tools: Any = None
    ) -> str:
        self.executed.append(task)
        _emit_fake_llm_call()
        return "ok"


class _StubTask:
    def __init__(
        self,
        description: str,
        *,
        name: Optional[str] = None,
        agent: Optional[_StubAgent] = None,
    ) -> None:
        self.description = description
        self.name = name
        self.agent = agent

    def execute_sync(
        self, agent: Any = None, context: Any = None, tools: Any = None
    ) -> str:
        return (agent or self.agent).execute_task(task=self)


class _StubCrew:
    def __init__(
        self, agents: list[_StubAgent], tasks: list[_StubTask]
    ) -> None:
        self.agents = agents
        self.tasks = tasks
        self.kickoffs: list[Any] = []

    def kickoff(self, inputs: Any = None) -> list[str]:
        self.kickoffs.append(inputs)
        return [
            t.execute_sync(agent=t.agent or self.agents[0])
            for t in self.tasks
        ]

    async def kickoff_async(self, inputs: Any = None) -> list[str]:
        return self.kickoff(inputs)


def _two_agent_crew() -> _StubCrew:
    researcher = _StubAgent(role="Researcher")
    writer = _StubAgent(role="Writer")
    return _StubCrew(
        agents=[researcher, writer],
        tasks=[
            _StubTask("Find the facts", name="research", agent=researcher),
            _StubTask("Write the post", name="draft", agent=writer),
        ],
    )


# ----------------------------------------------------------------------
# Entry-point wrapping
# ----------------------------------------------------------------------


def test_kickoff_is_scoped_under_an_agent_run() -> None:
    from inkfoot._instrument import _STORAGE

    crew = _two_agent_crew()
    instrument(crew, task="crew-test")
    crew.kickoff()

    rows = list(_STORAGE._conn().execute("SELECT task, status FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "crew-test"
    assert rows[0]["status"] == "complete"


def test_kickoff_async_is_scoped_and_reentrant_with_sync_kickoff() -> None:
    """``kickoff_async`` delegates to the (also wrapped) ``kickoff``
    — the reentrancy guard must still produce exactly one run."""
    from inkfoot._instrument import _STORAGE

    crew = _two_agent_crew()
    instrument(crew, task="crew-async")
    asyncio.run(crew.kickoff_async())

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0]["task"] == "crew-async"


def test_default_task_label_is_crewai() -> None:
    from inkfoot._instrument import _STORAGE

    crew = _two_agent_crew()
    instrument(crew)
    crew.kickoff()

    rows = list(_STORAGE._conn().execute("SELECT task FROM runs"))  # type: ignore[union-attr]
    assert rows[0]["task"] == "crewai"


def test_instrument_is_idempotent() -> None:
    crew = _two_agent_crew()
    h1 = instrument(crew, task="t")
    h2 = instrument(crew, task="t")
    assert h1 is h2


# ----------------------------------------------------------------------
# Multi-agent attribution
# ----------------------------------------------------------------------


def test_llm_calls_carry_agent_name_and_task_name_metadata() -> None:
    from inkfoot._instrument import _STORAGE

    crew = _two_agent_crew()
    instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call' "
        "ORDER BY sequence"
    )
    metas = [
        json.loads(r["payload_json"])["metadata"] for r in cur.fetchall()
    ]
    assert len(metas) == 2
    assert metas[0]["agent_name"] == "Researcher"
    assert metas[0]["task_name"] == "research"
    assert metas[1]["agent_name"] == "Writer"
    assert metas[1]["task_name"] == "draft"


def test_agent_label_prefers_name_over_role() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent(role="Researcher", name="ada")
    crew = _StubCrew(
        agents=[agent], tasks=[_StubTask("Find facts", agent=agent)]
    )
    instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call'"
    )
    meta = json.loads(cur.fetchone()["payload_json"])["metadata"]
    assert meta["agent_name"] == "ada"


def test_task_label_falls_back_to_collapsed_truncated_description() -> None:
    from inkfoot._instrument import _STORAGE

    agent = _StubAgent(role="Researcher")
    description = "Gather   facts\nabout the topic " + "x" * 200
    crew = _StubCrew(
        agents=[agent], tasks=[_StubTask(description, agent=agent)]
    )
    instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call'"
    )
    meta = json.loads(cur.fetchone()["payload_json"])["metadata"]
    assert meta["task_name"].startswith("Gather facts about the topic")
    assert "\n" not in meta["task_name"]
    assert len(meta["task_name"]) <= 80


def test_nested_agent_delegation_restores_outer_agent_name() -> None:
    """A manager-style agent that delegates to a worker mid-task gets
    its own label back once the worker returns."""
    from inkfoot._instrument import _STORAGE

    worker = _StubAgent(role="Worker")

    class _ManagerAgent(_StubAgent):
        def execute_task(
            self, task: Any = None, context: Any = None, tools: Any = None
        ) -> str:
            _emit_fake_llm_call()  # labelled Manager
            worker.execute_task(task=task)  # labelled Worker
            _emit_fake_llm_call()  # labelled Manager again
            return "ok"

    manager = _ManagerAgent(role="Manager")
    crew = _StubCrew(
        agents=[manager, worker],
        tasks=[_StubTask("Coordinate", name="coordinate", agent=manager)],
    )
    instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call' "
        "ORDER BY sequence"
    )
    agent_names = [
        json.loads(r["payload_json"])["metadata"]["agent_name"]
        for r in cur.fetchall()
    ]
    assert agent_names == ["Manager", "Worker", "Manager"]


def test_manager_agent_attribute_is_wrapped_when_present() -> None:
    from inkfoot._instrument import _STORAGE

    manager = _StubAgent(role="Manager")
    worker = _StubAgent(role="Worker")
    crew = _StubCrew(
        agents=[worker],
        tasks=[_StubTask("Oversee", name="oversee", agent=manager)],
    )
    crew.manager_agent = manager
    instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call'"
    )
    meta = json.loads(cur.fetchone()["payload_json"])["metadata"]
    assert meta["agent_name"] == "Manager"


def test_no_attribution_outside_wrapped_kickoff() -> None:
    """Execute methods called with no active run must not crash and
    must emit nothing."""
    from inkfoot._instrument import _STORAGE

    crew = _two_agent_crew()
    instrument(crew, task="t")
    # Directly poke the wrapped agent method outside any run scope.
    with pytest.raises(AssertionError):
        # _emit_fake_llm_call asserts there's an active run — proving
        # the scope helper didn't open one as a side effect.
        crew.agents[0].execute_task(task=crew.tasks[0])

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) AS n FROM events WHERE kind='llm_call'"
    )
    assert cur.fetchone()["n"] == 0


# ----------------------------------------------------------------------
# pydantic-style objects (real CrewAI rejects unknown setattr)
# ----------------------------------------------------------------------


class _RejectingSetattr:
    """Mimics pydantic v2 ``BaseModel.__setattr__``: unknown public
    attribute assignment raises ``ValueError``; underscore names go
    through plain ``object.__setattr__``."""

    _allowed_fields: tuple[str, ...] = ()

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("_") and name not in self._allowed_fields:
            raise ValueError(
                f'"{type(self).__name__}" object has no field "{name}"'
            )
        object.__setattr__(self, name, value)


class _PydanticishAgent(_RejectingSetattr):
    role = "Writer"

    def execute_task(
        self, task: Any = None, context: Any = None, tools: Any = None
    ) -> str:
        _emit_fake_llm_call()
        return "ok"


class _PydanticishTask(_RejectingSetattr):
    name = "draft"
    description = "Write the post"

    def execute_sync(
        self, agent: Any = None, context: Any = None, tools: Any = None
    ) -> str:
        return agent.execute_task(task=self)


class _PydanticishCrew(_RejectingSetattr):
    _allowed_fields = ("agents", "tasks")

    def __init__(self) -> None:
        self.agents = [_PydanticishAgent()]
        self.tasks = [_PydanticishTask()]

    def kickoff(self, inputs: Any = None) -> list[str]:
        return [
            t.execute_sync(agent=self.agents[0]) for t in self.tasks
        ]


def test_pydantic_style_crew_instruments_via_setattr_fallback() -> None:
    from inkfoot._instrument import _STORAGE

    crew = _PydanticishCrew()
    with pytest.raises(ValueError):
        crew.kickoff = lambda: None  # plain setattr really is refused

    inst = instrument(crew, task="t")
    crew.kickoff()

    cur = _STORAGE._conn().execute(  # type: ignore[union-attr]
        "SELECT payload_json FROM events WHERE kind='llm_call'"
    )
    meta = json.loads(cur.fetchone()["payload_json"])["metadata"]
    assert meta["agent_name"] == "Writer"
    assert meta["task_name"] == "draft"

    inst.shutdown()
    assert "kickoff" not in crew.__dict__
    assert "execute_task" not in crew.agents[0].__dict__
    # The restored crew still works.
    crew_runs = list(_STORAGE._conn().execute("SELECT id FROM runs"))  # type: ignore[union-attr]
    assert len(crew_runs) == 1


# ----------------------------------------------------------------------
# Report integration: --group-by metadata.<key>
# ----------------------------------------------------------------------


def test_report_group_by_metadata_agent_name_slices_per_agent(
    _isolated_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from inkfoot._instrument import _STORAGE
    from inkfoot.cli.report import run as report_run

    crew = _two_agent_crew()
    instrument(crew, task="t")
    crew.kickoff()

    run_id = _STORAGE._conn().execute("SELECT id FROM runs").fetchone()["id"]  # type: ignore[union-attr]
    rc = report_run(
        SimpleNamespace(
            db=str(_isolated_state),
            run=run_id,
            last=None,
            task=None,
            group_by="metadata.agent_name",
            show_zero=False,
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "per-agent_name ledger" in out
    assert "Researcher" in out
    assert "Writer" in out


def test_report_group_by_metadata_task_name_slices_per_task(
    _isolated_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from inkfoot._instrument import _STORAGE
    from inkfoot.cli.report import run as report_run

    crew = _two_agent_crew()
    instrument(crew, task="t")
    crew.kickoff()

    run_id = _STORAGE._conn().execute("SELECT id FROM runs").fetchone()["id"]  # type: ignore[union-attr]
    rc = report_run(
        SimpleNamespace(
            db=str(_isolated_state),
            run=run_id,
            last=None,
            task=None,
            group_by="metadata.task_name",
            show_zero=False,
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "research" in out
    assert "draft" in out


# ----------------------------------------------------------------------
# Capability declaration + registry
# ----------------------------------------------------------------------


def test_adapter_activates_on_instrument() -> None:
    crew = _two_agent_crew()
    instrument(crew, task="t")
    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == "crewai"


def test_supported_policies_is_empty_observation_only() -> None:
    assert CrewAIAdapter().supported_policies() == set()


def test_shutdown_restores_originals_and_is_idempotent() -> None:
    crew = _two_agent_crew()
    inst = instrument(crew, task="t")
    assert "kickoff" in crew.__dict__

    inst.shutdown()
    assert "kickoff" not in crew.__dict__
    assert "execute_task" not in crew.agents[0].__dict__
    assert "execute_sync" not in crew.tasks[0].__dict__
    inst.shutdown()  # idempotent


def test_instrumentation_shutdown_auto_deactivates_when_last_handle_closes() -> None:
    crew = _two_agent_crew()
    inst = instrument(crew, task="t")
    assert AdapterRegistry.get_active() is not None

    inst.shutdown()
    assert AdapterRegistry.get_active() is None
