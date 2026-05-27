"""Top-level re-export: ``inkfoot.anthropic_agent.instrument(agent)``.

See :mod:`inkfoot.adapters.anthropic_agent`."""

from __future__ import annotations

from inkfoot.adapters.anthropic_agent import (
    AnthropicAgentAdapter,
    instrument,
)

__all__ = ["AnthropicAgentAdapter", "instrument"]
