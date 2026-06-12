"""Outcome helpers — glue between framework results and
:func:`inkfoot.set_outcome`.

The aggregate report's headline column is cost-per-success, which
only works when runs report outcomes. For agents where "did it
work?" is mechanically visible in the framework's return value,
:func:`set_outcome_from_heuristic` saves writing the same
boilerplate mapping in every entry point.
"""

from inkfoot.outcomes._heuristics import set_outcome_from_heuristic

__all__ = ["set_outcome_from_heuristic"]
