"""Top-level re-export: ``inkfoot.openai_agents.instrument(agent)``.

See :mod:`inkfoot.adapters.openai_agents`."""

from __future__ import annotations

from inkfoot.adapters.openai_agents import (
    OpenAIAgentsAdapter,
    instrument,
)

__all__ = ["OpenAIAgentsAdapter", "instrument"]
