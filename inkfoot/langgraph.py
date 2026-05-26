"""Top-level re-export: ``inkfoot.langgraph.instrument(graph)``.

The implementation lives in :mod:`inkfoot.adapters.langgraph`; this
shim is the user-visible entry point named in the docs and the
quickstart::

    import inkfoot
    from inkfoot import langgraph as inkfoot_langgraph

    inkfoot.instrument()
    inkfoot_langgraph.instrument(graph, task="customer-support-triage")

Equivalent to::

    from inkfoot.adapters.langgraph import instrument
"""

from __future__ import annotations

from inkfoot.adapters.langgraph import (
    LangGraphAdapter,
    instrument,
)

__all__ = ["LangGraphAdapter", "instrument"]
