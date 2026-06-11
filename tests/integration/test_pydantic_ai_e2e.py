"""Pydantic AI end-to-end integration test.

Skips when the SDK isn't installed (``inkfoot[pydantic-ai]`` extra is
optional). Pydantic AI ships ``TestModel`` — a stub model that
generates plausible responses without any network call — so unlike
the other framework e2e tests this one can drive a real
``Agent.run_sync`` and assert the run scope lands, all without live
LLM cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pydantic_ai_mod = pytest.importorskip(
    "pydantic_ai",
    reason="Pydantic AI not installed — install inkfoot[pydantic-ai].",
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


def _build_test_agent() -> Any:
    """Construct an Agent backed by TestModel, skipping if the SDK
    surface has drifted from the expected shape."""
    Agent = getattr(pydantic_ai_mod, "Agent", None)
    if Agent is None:
        pytest.skip("pydantic_ai.Agent not exposed by this SDK version")
    try:
        from pydantic_ai.models.test import TestModel
    except ImportError:
        pytest.skip("pydantic_ai.models.test.TestModel not available")
    try:
        return Agent(TestModel())
    except TypeError:
        pytest.skip("Agent constructor signature differs from expected shape")


def test_pydantic_ai_run_sync_scopes_under_agent_run(
    instrumented: Path,
) -> None:
    from inkfoot._instrument import _STORAGE
    from inkfoot.adapters._registry import AdapterRegistry
    from inkfoot.pydantic_ai import instrument as pai_instrument

    agent = _build_test_agent()
    pai_instrument(agent, task="pai-e2e")

    assert AdapterRegistry.get_active() is not None
    assert AdapterRegistry.get_active().name == "pydantic_ai"

    # TestModel never leaves the process, so this is safe in CI.
    result = agent.run_sync("say ok")
    assert result is not None

    rows = list(
        _STORAGE._conn().execute(  # type: ignore[union-attr]
            "SELECT task, status FROM runs"
        )
    )
    assert len(rows) == 1
    assert rows[0]["task"] == "pai-e2e"
    assert rows[0]["status"] == "complete"


def test_pydantic_ai_shutdown_restores_run_sync(instrumented: Path) -> None:
    from inkfoot.pydantic_ai import instrument as pai_instrument

    agent = _build_test_agent()
    original = agent.run_sync
    handle = pai_instrument(agent, task="pai-e2e")
    assert agent.run_sync is not original

    handle.shutdown()
    assert "run_sync" not in agent.__dict__
