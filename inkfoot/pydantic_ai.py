"""Top-level re-export: ``inkfoot.pydantic_ai.instrument(agent)``.

The implementation lives in :mod:`inkfoot.adapters.pydantic_ai`; this
shim is the user-visible entry point named in the docs::

    import inkfoot
    import inkfoot.pydantic_ai

    inkfoot.instrument()
    inkfoot.pydantic_ai.instrument(agent, task="support-triage")

Equivalent to::

    from inkfoot.adapters.pydantic_ai import instrument
"""

from __future__ import annotations

from inkfoot.adapters.pydantic_ai import (
    PydanticAIAdapter,
    instrument,
)

__all__ = ["PydanticAIAdapter", "instrument"]
