"""E1-S4 T2 — Anthropic Agent SDK end-to-end test.

Mirrors the OpenAI Agents e2e test shape. Skips when the SDK isn't
installed (``inkfoot[anthropic-agent]`` extra is optional).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

anthropic_agent_mod = pytest.importorskip(
    "anthropic_agent",
    reason="Anthropic Agent SDK not installed — install inkfoot[anthropic-agent].",
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


def test_anthropic_agent_smoke_install(instrumented: Path) -> None:
    """Smoke test: the adapter installs against the real SDK
    surface without crashing and activates the registry pointer."""
    from inkfoot.anthropic_agent import instrument as anth_instrument

    Agent = getattr(anthropic_agent_mod, "Agent", None)
    if Agent is None:
        pytest.skip(
            "anthropic_agent.Agent attribute not exposed by this SDK version"
        )

    try:
        agent = Agent(name="test", instructions="say 'ok'")
    except TypeError:
        pytest.skip("Agent constructor signature differs from expected shape")

    anth_instrument(agent, task="anth-e2e")

    from inkfoot.adapters._registry import AdapterRegistry

    active = AdapterRegistry.get_active()
    assert active is not None
    assert active.name == "anthropic_agent"
