"""Token Contract schema — the typed shape of a contract YAML file.

A Token Contract is a small, declarative file that states the budget
and outcome a task is expected to hold to, plus a *degrade ladder*
describing what should happen as a run approaches its budget ceiling.
Contracts are code: they live in the repository, are reviewed in pull
requests, and are version-controlled alongside the agent they govern.

The grammar is intentionally narrow::

    schema_version: 1
    task: customer-support-triage
    cheap_model: claude-haiku-4-5
    budget:
      max_nanodollars: 50_000_000
      max_llm_calls: 8
      max_tool_result_tokens: 1500
      cache_hit_rate_min: 0.70
      max_run_duration_seconds: 30
    outcome:
      required_success_rate: 0.95
      measure_window_runs: 100
    degrade:
      - at_percent: 80
        action: warn
      - at_percent: 90
        action: switch_to_cheap_model
      - at_percent: 100
        action: block
    overrides:
      free_tier:
        budget:
          max_nanodollars: 10_000_000

The schema is expressed as frozen dataclasses with explicit
``from_dict`` validation rather than a third-party model library. This
keeps the runtime dependency footprint flat — a contract loads with
nothing beyond a YAML parser — while still rejecting typos, missing
fields, and impossible thresholds at load time. Unknown keys are
rejected everywhere (no silently-ignored fields), so a misspelled
``max_nanodolars`` fails loudly instead of disabling a budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from inkfoot.errors import InkfootError


# The schema version this build writes and treats as current. The
# loader also accepts the immediately-preceding version (current - 1)
# with a deprecation warning; anything older is rejected.
CONTRACT_SCHEMA_VERSION = 1


class ContractValidationError(InkfootError):
    """Raised when a contract document is structurally invalid.

    The message names the offending field and, where available, the
    source file and the value that failed — enough for a developer to
    fix the YAML without guessing.
    """


class DegradeAction(str, Enum):
    """The fixed set of actions a degrade step may request.

    The set is deliberately closed: the enforcer never improvises an
    action, so a contract can only ask for behaviour the runtime knows
    how to deliver deterministically.

    * ``warn`` — record a violation event; the call proceeds unchanged.
    * ``switch_to_cheap_model`` — rewrite the call's model to the
      contract's cheaper fallback; the call proceeds on that model.
    * ``block`` — refuse the call; the SDK request is never made.
    """

    WARN = "warn"
    SWITCH_TO_CHEAP_MODEL = "switch_to_cheap_model"
    BLOCK = "block"


@dataclass(frozen=True)
class DegradeStep:
    """One rung of the degrade ladder: an action at a budget percentage."""

    at_percent: int
    action: DegradeAction


@dataclass(frozen=True)
class BudgetClause:
    """Per-run spending and shape limits.

    Every field is optional; a contract states only the limits it
    cares about. ``None`` means "no limit on this dimension".
    """

    max_nanodollars: Optional[int] = None
    max_llm_calls: Optional[int] = None
    max_tool_result_tokens: Optional[int] = None
    cache_hit_rate_min: Optional[float] = None
    max_run_duration_seconds: Optional[int] = None

    def merge(self, other: "BudgetClause") -> "BudgetClause":
        """Return a copy with every set field of ``other`` overriding
        this clause. Used to resolve per-tier overrides over the base."""
        return BudgetClause(
            max_nanodollars=_pick(other.max_nanodollars, self.max_nanodollars),
            max_llm_calls=_pick(other.max_llm_calls, self.max_llm_calls),
            max_tool_result_tokens=_pick(
                other.max_tool_result_tokens, self.max_tool_result_tokens
            ),
            cache_hit_rate_min=_pick(
                other.cache_hit_rate_min, self.cache_hit_rate_min
            ),
            max_run_duration_seconds=_pick(
                other.max_run_duration_seconds, self.max_run_duration_seconds
            ),
        )


@dataclass(frozen=True)
class OutcomeClause:
    """Quality expectation measured across a window of recent runs.

    Outcome clauses are advisory: they surface in reports and emit
    runtime violation events, but they never block a call or fail a CI
    build. A benchmark scenario can't measure production outcome
    quality, so treating an outcome shortfall as a hard gate would
    either rubber-stamp every run or punish unrelated drift.
    """

    required_success_rate: Optional[float] = None
    measure_window_runs: int = 100

    def merge(self, other: "OutcomeClause") -> "OutcomeClause":
        return OutcomeClause(
            required_success_rate=_pick(
                other.required_success_rate, self.required_success_rate
            ),
            measure_window_runs=_pick(
                other.measure_window_runs, self.measure_window_runs
            ),
        )


@dataclass(frozen=True)
class Override:
    """A per-tier override of the base budget/outcome clauses.

    Resolved against the run's ``metadata.tenant_tier``; an absent or
    non-matching tier falls back to the top-level clauses.
    """

    budget: Optional[BudgetClause] = None
    outcome: Optional[OutcomeClause] = None


@dataclass(frozen=True)
class Contract:
    """A fully-validated Token Contract for a single task."""

    schema_version: int
    task: str
    budget: Optional[BudgetClause] = None
    outcome: Optional[OutcomeClause] = None
    degrade: tuple[DegradeStep, ...] = ()
    cheap_model: Optional[str] = None
    overrides: Mapping[str, Override] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolved_budget(self, tier: Optional[str] = None) -> Optional[BudgetClause]:
        """Return the budget clause that applies to ``tier``.

        A matching override's fields layer over the base budget; an
        absent or unknown tier returns the base unchanged.
        """
        base = self.budget
        if tier is None:
            return base
        override = self.overrides.get(tier)
        if override is None or override.budget is None:
            return base
        if base is None:
            return override.budget
        return base.merge(override.budget)

    def resolved_outcome(self, tier: Optional[str] = None) -> Optional[OutcomeClause]:
        base = self.outcome
        if tier is None:
            return base
        override = self.overrides.get(tier)
        if override is None or override.outcome is None:
            return base
        if base is None:
            return override.outcome
        return base.merge(override.outcome)


# ----------------------------------------------------------------------
# Parsing + validation
# ----------------------------------------------------------------------


def contract_from_dict(
    raw: Mapping[str, Any], *, source: Optional[str] = None
) -> Contract:
    """Build and validate a :class:`Contract` from a parsed mapping.

    Raises :class:`ContractValidationError` on any structural problem:
    a missing or mistyped field, an unknown key, an ``at_percent``
    outside 1–100, or a ``switch_to_cheap_model`` step with no
    ``cheap_model`` declared. ``source`` is woven into error messages
    so the developer knows which file to fix.
    """
    where = f" in {source}" if source else ""
    if not isinstance(raw, Mapping):
        raise ContractValidationError(
            f"contract{where} must be a YAML mapping, got "
            f"{type(raw).__name__}"
        )

    _reject_unknown(
        raw,
        allowed={
            "schema_version",
            "task",
            "budget",
            "outcome",
            "degrade",
            "cheap_model",
            "overrides",
        },
        context=f"contract{where}",
    )

    schema_version = _require_int(raw, "schema_version", where)
    task = _require_str(raw, "task", where)

    cheap_model = raw.get("cheap_model")
    if cheap_model is not None and not _is_nonempty_str(cheap_model):
        raise ContractValidationError(
            f"contract{where}: 'cheap_model' must be a non-empty string"
        )

    budget = _parse_budget(raw.get("budget"), where)
    outcome = _parse_outcome(raw.get("outcome"), where)
    degrade = _parse_degrade(raw.get("degrade"), where)
    overrides = _parse_overrides(raw.get("overrides"), where)

    contract = Contract(
        schema_version=schema_version,
        task=task,
        budget=budget,
        outcome=outcome,
        degrade=degrade,
        cheap_model=cheap_model,
        overrides=overrides,
    )

    _validate_cross_field(contract, where)
    return contract


def _validate_cross_field(contract: Contract, where: str) -> None:
    """Checks that span more than one field.

    A ``switch_to_cheap_model`` rung is only valid when the contract
    declares a ``cheap_model`` for the runtime to switch to. (The
    runtime can also fall back to a provider's default cheap model, but
    requiring the explicit declaration here keeps the contract
    self-documenting and catches the common omission at load time.)
    """
    wants_switch = any(
        step.action is DegradeAction.SWITCH_TO_CHEAP_MODEL
        for step in contract.degrade
    )
    if wants_switch and not contract.cheap_model:
        raise ContractValidationError(
            f"contract{where}: a 'switch_to_cheap_model' degrade step "
            f"requires a top-level 'cheap_model' to switch to, but none "
            f"is declared."
        )


def _parse_budget(raw: Any, where: str) -> Optional[BudgetClause]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractValidationError(f"contract{where}: 'budget' must be a mapping")
    _reject_unknown(
        raw,
        allowed={
            "max_nanodollars",
            "max_llm_calls",
            "max_tool_result_tokens",
            "cache_hit_rate_min",
            "max_run_duration_seconds",
        },
        context=f"budget{where}",
    )
    return BudgetClause(
        max_nanodollars=_opt_pos_int(raw, "max_nanodollars", where),
        max_llm_calls=_opt_pos_int(raw, "max_llm_calls", where),
        max_tool_result_tokens=_opt_nonneg_int(raw, "max_tool_result_tokens", where),
        cache_hit_rate_min=_opt_rate(raw, "cache_hit_rate_min", where),
        max_run_duration_seconds=_opt_nonneg_int(
            raw, "max_run_duration_seconds", where
        ),
    )


def _parse_outcome(raw: Any, where: str) -> Optional[OutcomeClause]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractValidationError(f"contract{where}: 'outcome' must be a mapping")
    _reject_unknown(
        raw,
        allowed={"required_success_rate", "measure_window_runs"},
        context=f"outcome{where}",
    )
    window = _opt_pos_int(raw, "measure_window_runs", where)
    return OutcomeClause(
        required_success_rate=_opt_rate(raw, "required_success_rate", where),
        measure_window_runs=window if window is not None else 100,
    )


def _parse_degrade(raw: Any, where: str) -> tuple[DegradeStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ContractValidationError(
            f"contract{where}: 'degrade' must be a list of steps"
        )
    steps: list[DegradeStep] = []
    seen_percents: set[int] = set()
    for index, item in enumerate(raw):
        loc = f"{where} (degrade step {index})"
        if not isinstance(item, Mapping):
            raise ContractValidationError(
                f"contract{loc}: each degrade step must be a mapping"
            )
        _reject_unknown(
            item, allowed={"at_percent", "action"}, context=f"degrade step{loc}"
        )
        at_percent = _require_int(item, "at_percent", loc)
        if not 1 <= at_percent <= 100:
            raise ContractValidationError(
                f"contract{loc}: 'at_percent' must be between 1 and 100, "
                f"got {at_percent}"
            )
        if at_percent in seen_percents:
            raise ContractValidationError(
                f"contract{loc}: duplicate degrade step at_percent={at_percent}"
            )
        seen_percents.add(at_percent)
        action_raw = item.get("action")
        try:
            action = DegradeAction(action_raw)
        except ValueError:
            valid = ", ".join(a.value for a in DegradeAction)
            raise ContractValidationError(
                f"contract{loc}: unknown action {action_raw!r}; "
                f"expected one of: {valid}"
            )
        steps.append(DegradeStep(at_percent=at_percent, action=action))
    # Keep the ladder sorted by threshold so the enforcer can walk it
    # deterministically regardless of authoring order.
    steps.sort(key=lambda s: s.at_percent)
    return tuple(steps)


def _parse_overrides(raw: Any, where: str) -> Mapping[str, Override]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ContractValidationError(
            f"contract{where}: 'overrides' must be a mapping of tier -> clauses"
        )
    out: dict[str, Override] = {}
    for tier, body in raw.items():
        if not _is_nonempty_str(tier):
            raise ContractValidationError(
                f"contract{where}: override tier names must be non-empty strings"
            )
        loc = f"{where} (override {tier!r})"
        if not isinstance(body, Mapping):
            raise ContractValidationError(
                f"contract{loc}: override body must be a mapping"
            )
        _reject_unknown(
            body, allowed={"budget", "outcome"}, context=f"override{loc}"
        )
        out[tier] = Override(
            budget=_parse_budget(body.get("budget"), loc),
            outcome=_parse_outcome(body.get("outcome"), loc),
        )
    return out


# ----------------------------------------------------------------------
# Small field helpers
# ----------------------------------------------------------------------


def _pick(preferred: Any, fallback: Any) -> Any:
    return preferred if preferred is not None else fallback


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _reject_unknown(raw: Mapping[str, Any], *, allowed: set[str], context: str) -> None:
    extra = set(raw.keys()) - allowed
    if extra:
        names = ", ".join(sorted(str(k) for k in extra))
        raise ContractValidationError(
            f"{context}: unknown field(s): {names}. Allowed: "
            f"{', '.join(sorted(allowed))}."
        )


def _require_int(raw: Mapping[str, Any], key: str, where: str) -> int:
    if key not in raw:
        raise ContractValidationError(f"contract{where}: missing required '{key}'")
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise ContractValidationError(
            f"contract{where}: '{key}' must be an integer, got {val!r}"
        )
    return val


def _require_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    if key not in raw:
        raise ContractValidationError(f"contract{where}: missing required '{key}'")
    val = raw[key]
    if not _is_nonempty_str(val):
        raise ContractValidationError(
            f"contract{where}: '{key}' must be a non-empty string, got {val!r}"
        )
    return val


def _opt_nonneg_int(raw: Mapping[str, Any], key: str, where: str) -> Optional[int]:
    if raw.get(key) is None:
        return None
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise ContractValidationError(
            f"contract{where}: '{key}' must be a non-negative integer, got {val!r}"
        )
    return val


def _opt_pos_int(raw: Mapping[str, Any], key: str, where: str) -> Optional[int]:
    if raw.get(key) is None:
        return None
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise ContractValidationError(
            f"contract{where}: '{key}' must be a positive integer, got {val!r}"
        )
    return val


def _opt_rate(raw: Mapping[str, Any], key: str, where: str) -> Optional[float]:
    if raw.get(key) is None:
        return None
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ContractValidationError(
            f"contract{where}: '{key}' must be a number in [0, 1], got {val!r}"
        )
    f = float(val)
    if not 0.0 <= f <= 1.0:
        raise ContractValidationError(
            f"contract{where}: '{key}' must be within [0, 1], got {f}"
        )
    return f
