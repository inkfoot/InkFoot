"""Framework adapter package — the framework-adapter surface.

The current release ships five adapters:

* :mod:`inkfoot.adapters.langgraph` — the headline LangGraph adapter
  with per-node attribution + tools-fingerprint capture.
* :mod:`inkfoot.adapters.openai_agents` — wraps ``Agent.run`` and the
  tool-dispatch layer.
* :mod:`inkfoot.adapters.anthropic_agent` — mirrors the OpenAI Agents
  adapter for Anthropic's Agent SDK.
* :mod:`inkfoot.adapters.pydantic_ai` — wraps ``Agent.run`` /
  ``run_sync`` plus the registered-tool layer.
* :mod:`inkfoot.adapters.crewai` — wraps ``Crew.kickoff`` and stamps
  per-agent / per-task attribution metadata (observation-only).

:class:`~inkfoot.adapters.base.FrameworkAdapter` is the Protocol
every adapter satisfies, plus :data:`AdapterRegistry` which
``instrument()`` calls into for capability propagation.

The top-level convenience modules — :mod:`inkfoot.langgraph`,
:mod:`inkfoot.openai_agents`, :mod:`inkfoot.anthropic_agent`,
:mod:`inkfoot.pydantic_ai`, :mod:`inkfoot.crewai` — are thin
re-exports of each adapter's ``instrument()``.
"""

from __future__ import annotations

from inkfoot.adapters._registry import (
    AdapterRegistry,
    DuplicateAdapterName,
    get_active_adapter,
)
from inkfoot.adapters.base import FrameworkAdapter, Instrumentation

__all__ = [
    "AdapterRegistry",
    "DuplicateAdapterName",
    "FrameworkAdapter",
    "Instrumentation",
    "get_active_adapter",
]
