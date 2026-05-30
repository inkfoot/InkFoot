"""Smell engine — the wedge.

Instrumentation alone is commodity. The first "aha" report is what
makes Inkfoot useful, and the smells are how a reader looking at a
bar chart sees *why* their tokens are doing what they're doing.

The current implementation ships five smells. Each is **data, not code** — a frozen
:class:`CostSmell` carries the id, title, description, severity, a
``detect(run, events) -> DetectionResult | None`` callable, the
recommendation text, the suggested follow-up policy, and an
``evidence_query`` string that reproduces the evidence in SQL/JSONPath
so the user can audit a finding without running the engine again.

The engine itself (:class:`~inkfoot.smells.engine.SmellEngine`) lives
in a sibling module to keep this file thin. Concrete smells live in
their own files; this module re-exports them as the
:data:`DEFAULT_SMELLS` list.

See ``the architecture notes`` §5.9 for the authoritative shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.run import Run


__all__ = [
    "CostSmell",
    "DetectionResult",
    "Severity",
    "DEFAULT_SMELLS",
    "register_smell",
    "get_smell",
    "list_smells",
    "clear_registry",
    "iter_llm_calls",
]


Severity = Literal["info", "warn", "critical"]
_VALID_SEVERITIES: frozenset[str] = frozenset({"info", "warn", "critical"})


@dataclass(frozen=True, slots=True)
class CostSmell:
    """One named cost smell — data, not code.

    ``detect`` is the smell-specific predicate. It receives the run
    metadata and the iterable of events for the run (Storage rows as
    dicts, per :meth:`SQLiteStorage.iter_events`) and returns a
    populated :class:`DetectionResult` when it fires, or ``None`` when
    the run is clean.

    The ``severity`` on the dataclass is the *default* severity; a
    detector can downgrade an individual hit (e.g. critical → warn)
    by setting the result's own ``severity`` to a softer level.
    """

    id: str
    title: str
    description: str
    severity: Severity
    detect: Callable[["Run", Iterable[dict[str, Any]]], Optional["DetectionResult"]]
    recommendation: str
    suggested_policy: Optional[str] = None
    evidence_query: str = ""
    # Optional ledger field this smell primarily explains. The
    # ``inkfoot report`` renderer puts a "⚠" marker on the matching
    # bar-chart row. ``None`` means the smell doesn't anchor to a
    # single category (``runaway-retry-loop`` is a run-shape
    # pattern, not a category problem).
    primary_category: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"CostSmell({self.id!r}): severity must be one of "
                f"{sorted(_VALID_SEVERITIES)}, got {self.severity!r}"
            )
        if not self.id:
            raise ValueError("CostSmell: id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """One hit from one smell on one run.

    The renderer joins this to its :class:`CostSmell` to produce the
    "Smells detected" section of the report. ``evidence`` is a
    smell-specific dict; ``estimated_cost_impact_nd`` is integer
    nanodollars so the renderer can format consistently with the
    attribution bar chart.

    ``triggered_at_sequence`` anchors the finding to a specific event
    in the run's log; reports use this to print "triggered at turn N"
    and dashboards can link directly to the offending event.
    """

    smell: CostSmell
    triggered_at_sequence: int
    severity: Severity
    evidence: dict[str, Any] = field(default_factory=dict)
    estimated_cost_impact_nd: int = 0

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"DetectionResult: severity must be one of "
                f"{sorted(_VALID_SEVERITIES)}, got {self.severity!r}"
            )


# ----------------------------------------------------------------------
# Event-stream helper
# ----------------------------------------------------------------------


def iter_llm_calls(
    events: Iterable[dict[str, Any]],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield ``(event_row, neutral_call_dict)`` pairs for every
    ``llm_call`` event in ``events``.

    ``payload_json`` was written by the shim's ``emit_llm_call`` as
    ``json.dumps(asdict(NeutralCall(...)))`` — we round-trip via
    ``json.loads`` here. Unparseable payloads are silently skipped
    (the shim's hook-isolation absorbs translator failures so we
    occasionally see partial rows; smells should not crash on them).

    Non-``llm_call`` events are skipped — policy events
    (``budget_warning`` etc.) and ``outcome`` events have different
    payload shapes that smells don't currently consume.
    """
    import json as _json

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") != "llm_call":
            continue
        raw = ev.get("payload_json")
        if not raw:
            continue
        try:
            payload = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        yield ev, payload


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------

# Module-level registry of all known smells, keyed by id. The current implementation only
# registers the five current smells here; future aggregate analysis's community Cost
# Smell Library plugs into this same dict.
_registry: dict[str, CostSmell] = {}


def register_smell(smell: CostSmell) -> None:
    """Register a smell with the global registry.

    Rejects duplicate ids with :class:`ValueError`. Re-registering
    the *same instance* is treated as a duplicate too — there is no
    "is this object identity already there" shortcut, so callers
    don't accidentally rely on identity dedup.
    """
    if smell.id in _registry:
        raise ValueError(
            f"register_smell: smell id {smell.id!r} is already registered "
            f"(re-registration is not allowed; clear via clear_registry())"
        )
    _registry[smell.id] = smell


def get_smell(smell_id: str) -> CostSmell:
    """Return the smell with id ``smell_id``. Raises ``KeyError``
    when unknown."""
    try:
        return _registry[smell_id]
    except KeyError as exc:
        raise KeyError(
            f"get_smell: unknown smell id {smell_id!r}. "
            f"Known ids: {sorted(_registry)}"
        ) from exc


def list_smells() -> list[CostSmell]:
    """Snapshot of every registered smell, in registration order.

    Current callers use this to drive the engine; future aggregate analysis will use it
    to render the public Cost Smell Library catalogue."""
    return list(_registry.values())


def clear_registry() -> None:
    """Drop every registered smell. Tests use this to reset between
    cases; production code should never call it."""
    _registry.clear()


# Imports below populate the registry. Kept at the bottom so the
# concrete smell modules can ``from inkfoot.smells import CostSmell,
# DetectionResult`` without circulars.
from inkfoot.smells.unstable_prompt_prefix import UNSTABLE_PROMPT_PREFIX  # noqa: E402
from inkfoot.smells.runaway_retry_loop import RUNAWAY_RETRY_LOOP  # noqa: E402
from inkfoot.smells.oversized_tool_result_recycled import (  # noqa: E402
    OVERSIZED_TOOL_RESULT_RECYCLED,
)
from inkfoot.smells.expensive_model_low_entropy import (  # noqa: E402
    EXPENSIVE_MODEL_LOW_ENTROPY,
)
from inkfoot.smells.recurring_cache_writes import RECURRING_CACHE_WRITES  # noqa: E402

# The five current smells, in canonical reporting order.
DEFAULT_SMELLS: tuple[CostSmell, ...] = (
    UNSTABLE_PROMPT_PREFIX,
    RUNAWAY_RETRY_LOOP,
    OVERSIZED_TOOL_RESULT_RECYCLED,
    EXPENSIVE_MODEL_LOW_ENTROPY,
    RECURRING_CACHE_WRITES,
)

# Populate the registry once at module load. Tests can clear and
# repopulate via ``clear_registry`` + ``register_smell``.
for _smell in DEFAULT_SMELLS:
    register_smell(_smell)
