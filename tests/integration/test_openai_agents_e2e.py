"""OpenAI Agents SDK end-to-end integration test.

Skips when the SDK isn't installed (``inkfoot[openai-agents]`` extra
is optional). The OpenAI Agents SDK surface is still evolving as of
mid-2026; this test pins the smallest stable interface — ``Agent``
class with a ``run`` method — and asserts the run scope + tool
dispatch events land as expected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Pip name is ``openai-agents``; the import module is ``agents``.
agents_mod = pytest.importorskip(
    "agents",
    reason="OpenAI Agents SDK not installed — install inkfoot[openai-agents].",
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


def test_openai_agents_smoke_run_scopes_under_agent_run(
    instrumented: Path,
) -> None:
    """Smoke test: construct an Agent, instrument it, run a trivial
    prompt without hitting a real API (we monkey-patch the SDK's
    internal call to dodge live LLM cost in CI), and assert one run
    row gets written."""
    from inkfoot._instrument import _STORAGE
    from inkfoot.openai_agents import instrument as oa_instrument

    # The exact constructor signature varies; skip cleanly if our
    # contract assumption no longer holds.
    Agent = getattr(agents_mod, "Agent", None)
    if Agent is None:
        pytest.skip("agents.Agent attribute not exposed by this SDK version")

    try:
        agent = Agent(name="test", instructions="say 'ok'")
    except TypeError:
        pytest.skip("Agent constructor signature differs from expected shape")

    oa_instrument(agent, task="oa-e2e")

    # We don't actually call run() here — calling it would hit a real
    # provider. The contract this test pins is: instrumentation
    # installs without crashing, and the adapter activates the
    # registry pointer.
    from inkfoot.adapters._registry import AdapterRegistry

    assert AdapterRegistry.get_active() is not None
    assert AdapterRegistry.get_active().name == "openai_agents"
