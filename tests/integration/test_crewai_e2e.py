"""CrewAI end-to-end integration test.

Skips when the SDK isn't installed (``inkfoot[crewai]`` extra is
optional). CrewAI has no offline stub model, so this test never calls
``kickoff()`` — that would hit a live provider. The contract it pins
is the part that breaks when CrewAI's surface drifts: real Crew /
Agent / Task objects are pydantic models that reject plain attribute
assignment, so instrumentation must install (and shutdown must
restore) through the ``object.__setattr__`` fallback without
crashing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

crewai_mod = pytest.importorskip(
    "crewai",
    reason="CrewAI not installed — install inkfoot[crewai].",
)


@pytest.fixture()
def instrumented(tmp_path: Path) -> Any:
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


def _build_crew() -> Any:
    """Construct a minimal one-agent crew, skipping if the SDK
    surface has drifted from the expected shape."""
    Agent = getattr(crewai_mod, "Agent", None)
    Task = getattr(crewai_mod, "Task", None)
    Crew = getattr(crewai_mod, "Crew", None)
    if not all((Agent, Task, Crew)):
        pytest.skip("crewai does not expose Agent/Task/Crew")
    try:
        agent = Agent(
            role="Researcher",
            goal="Find facts",
            backstory="A diligent researcher.",
        )
        task = Task(
            description="Collect three facts about ink.",
            expected_output="Three bullet points.",
            agent=agent,
        )
        return Crew(agents=[agent], tasks=[task])
    except Exception as exc:  # pydantic ValidationError, TypeError, ...
        pytest.skip(f"Crew construction failed on this SDK version: {exc}")


def test_crewai_instrument_installs_on_real_pydantic_models(
    instrumented: Path,
) -> None:
    from inkfoot.adapters._registry import AdapterRegistry
    from inkfoot.crewai import instrument as crewai_instrument

    crew = _build_crew()
    crewai_instrument(crew, task="crewai-e2e")

    assert AdapterRegistry.get_active() is not None
    assert AdapterRegistry.get_active().name == "crewai"
    # The wrapped kickoff must shadow the class method via the
    # instance __dict__ (pydantic rejects plain setattr).
    assert "kickoff" in crew.__dict__

    # We don't call kickoff() — that would hit a real provider.


def test_crewai_shutdown_restores_kickoff_and_hooks(
    instrumented: Path,
) -> None:
    from inkfoot.crewai import instrument as crewai_instrument

    crew = _build_crew()
    handle = crewai_instrument(crew, task="crewai-e2e")
    agent = crew.agents[0]
    crew_task = crew.tasks[0]

    handle.shutdown()
    assert "kickoff" not in crew.__dict__
    assert "execute_task" not in agent.__dict__
    for name in ("execute_sync", "execute_async", "execute"):
        assert name not in crew_task.__dict__
