"""Top-level re-export: ``inkfoot.crewai.instrument(crew)``.

The implementation lives in :mod:`inkfoot.adapters.crewai`; this
shim is the user-visible entry point named in the docs::

    import inkfoot
    import inkfoot.crewai

    inkfoot.instrument()
    inkfoot.crewai.instrument(crew, task="research-pipeline")

Equivalent to::

    from inkfoot.adapters.crewai import instrument
"""

from __future__ import annotations

from inkfoot.adapters.crewai import (
    CrewAIAdapter,
    instrument,
)

__all__ = ["CrewAIAdapter", "instrument"]
