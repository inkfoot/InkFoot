"""Heuristic outcome inference from framework results.

:func:`set_outcome_from_heuristic` maps the value an agent
framework hands back into a :func:`inkfoot.set_outcome` call, for
the common cases where success is mechanically visible:

* LangGraph — ``graph.invoke(...)`` returning its final state dict
  means the graph reached END: success.
* OpenAI Agents SDK / Pydantic AI / CrewAI — result objects whose
  payload attribute (``final_output`` / ``output`` / ``data`` /
  ``raw``) is populated: success.
* An exception — whether passed positionally or via ``error=`` —
  is a failure.

The helper is deliberately conservative: when it can't tell (the
result is ``None``, falsy, or a result object with an empty
payload), it makes **no** ``set_outcome`` call and returns ``None``
so the run stays visibly uninstrumented rather than being guessed
into a bucket. ``accepted_answer`` and ``human_escalated`` always
require an explicit ``set_outcome`` — no heuristic can know a
human reviewed the answer.

Duck-typed on purpose: no framework imports, so it costs nothing
when the framework isn't installed.
"""

from __future__ import annotations

from typing import Any, Optional

from inkfoot._run_lifecycle import set_outcome


# Result-object payload attributes, checked in order: OpenAI Agents
# SDK RunResult.final_output, Pydantic AI AgentRunResult.output
# (and its older .data spelling), CrewAI CrewOutput.raw.
_RESULT_PAYLOAD_ATTRS = ("final_output", "output", "data", "raw")


def set_outcome_from_heuristic(
    result: Any = None,
    *,
    error: Optional[BaseException] = None,
) -> Optional[str]:
    """Infer the active run's outcome from a framework result and
    record it via :func:`inkfoot.set_outcome`.

    Returns the outcome string it recorded (``"success"`` /
    ``"failure"``), or ``None`` when nothing could be inferred —
    in which case no ``set_outcome`` call is made and the run
    stays in the report's uninstrumented bucket.

    Typical shape around a LangGraph entry point::

        with inkfoot.agent_run(task="triage"):
            try:
                state = graph.invoke(inputs)
            except Exception as exc:
                set_outcome_from_heuristic(error=exc)
                raise
            set_outcome_from_heuristic(state)

    Raises :class:`inkfoot.NoActiveRun` (via ``set_outcome``) when
    called outside an ``agent_run`` block *and* an outcome was
    inferred; the no-inference path never raises.
    """
    if error is not None or isinstance(result, BaseException):
        set_outcome("failure")
        return "failure"

    if result is None:
        return None

    # LangGraph: invoke() returns the final state mapping when the
    # graph reaches END. Even an empty state dict means the graph
    # ran to completion.
    if isinstance(result, dict):
        set_outcome("success")
        return "success"

    for attr in _RESULT_PAYLOAD_ATTRS:
        if hasattr(result, attr):
            if getattr(result, attr) is not None:
                set_outcome("success")
                return "success"
            # A result object with an empty payload is ambiguous —
            # don't guess.
            return None

    if result:
        set_outcome("success")
        return "success"
    return None
