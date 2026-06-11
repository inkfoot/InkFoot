"""Process-global wiring between the enforcer and the call hot path.

The shims call :func:`enforce_before_call` on every LLM call. Unlike
the observe-only policy hooks, this path is *not* run under exception
isolation: a ``block`` decision must be able to raise
:class:`~inkfoot.errors.PolicyBlocked` straight out to the caller so
the SDK request is never made. Keeping the enforcer here — rather than
registering it as a policy — is what makes that possible.

A single active enforcer exists per process (mirroring the single
active instrumentation). It is installed by ``inkfoot.instrument`` when
the caller passes ``contracts=[...]`` and torn down by ``shutdown``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

from inkfoot.contracts.enforcer import ContractEnforcer, EnforcementOutcome
from inkfoot.errors import PolicyBlocked

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.normalise import NeutralCall
    from inkfoot.policy import CallContext
    from inkfoot.storage import Storage

_LOG = logging.getLogger("inkfoot.contracts")

_lock = threading.Lock()
_active_enforcer: Optional[ContractEnforcer] = None
_active_storage: Optional["Storage"] = None
# Cache of run_id -> (task, tier) so the hot path doesn't hit storage on
# every call. Populated by :func:`on_run_start`; falls back to a storage
# read for runs that began outside an explicit ``agent_run``.
_run_facts: dict[str, tuple[Optional[str], Optional[str]]] = {}


def set_active_enforcer(enforcer: ContractEnforcer, storage: "Storage") -> None:
    global _active_enforcer, _active_storage
    with _lock:
        _active_enforcer = enforcer
        _active_storage = storage
        _run_facts.clear()


def clear_active_enforcer() -> None:
    global _active_enforcer, _active_storage
    with _lock:
        _active_enforcer = None
        _active_storage = None
        _run_facts.clear()


def get_active_enforcer() -> Optional[ContractEnforcer]:
    return _active_enforcer


# ----------------------------------------------------------------------
# Run lifecycle hooks
# ----------------------------------------------------------------------


def on_run_start(
    run_id: str, task: Optional[str], metadata: Optional[dict[str, Any]]
) -> None:
    enforcer = _active_enforcer
    if enforcer is None or task is None:
        return
    tier = _tier_from_metadata(metadata)
    with _lock:
        _run_facts[run_id] = (task, tier)
    if enforcer.has_contract(task):
        enforcer.register_run(run_id, task, tier=tier)


def on_run_end(run_id: str) -> None:
    enforcer = _active_enforcer
    if enforcer is not None:
        enforcer.release_run(run_id)
    with _lock:
        _run_facts.pop(run_id, None)


# ----------------------------------------------------------------------
# Per-call enforcement (the hot path)
# ----------------------------------------------------------------------


def enforce_before_call(ctx: "CallContext") -> None:
    """Evaluate the active contract for ``ctx``'s run before the call.

    Mutates ``ctx`` (and its ``request_kwargs``) in place for a
    model switch, and raises :class:`PolicyBlocked` for a block. A
    no-op when no enforcer is active or the run's task has no contract.
    """
    enforcer = _active_enforcer
    storage = _active_storage
    if enforcer is None or ctx is None:
        return

    # Policy helper calls (e.g. CheapSummariser's cheap-model call) are
    # infrastructure that exists to *reduce* spend — gating or blocking
    # them would silently degrade the policy exactly when the contract
    # is tightest. They are exempt from enforcement; their real spend
    # still folds into the run via record_call below.
    if _is_helper_call(ctx):
        return

    task, tier = _facts_for_run(ctx.run_id, storage)
    if not enforcer.has_contract(task):
        return

    outcome = enforcer.before_call(
        run_id=ctx.run_id,
        task=task,
        provider=ctx.provider,
        model=ctx.model,
        request_kwargs=ctx.request_kwargs,
        tier=tier,
    )
    _emit_violations(ctx.run_id, outcome, storage)

    if outcome.action == "switch_to_cheap_model" and outcome.new_model:
        ctx.request_kwargs["model"] = outcome.new_model
        ctx.model = outcome.new_model
    elif outcome.action == "block":
        violation = outcome.violations[0] if outcome.violations else None
        raise PolicyBlocked(
            _block_message(task, violation),
            clause=violation.clause_name if violation else None,
            projected=violation.projected_value if violation else None,
            threshold=violation.threshold if violation else None,
        )


def record_call(ctx: "CallContext", neutral_call: "NeutralCall") -> None:
    """Fold a completed call's actuals back into the enforcer.

    Called from the shared emit pipeline after the translator runs, so
    the running spend and the per-task output moving average stay
    current. Best-effort: never raises into the hot path.
    """
    enforcer = _active_enforcer
    if enforcer is None or ctx is None or neutral_call is None:
        return
    try:
        ledger = getattr(neutral_call, "ledger", None)
        output_tokens = getattr(ledger, "output_tokens", None) if ledger else None
        task, _ = _facts_for_run(ctx.run_id, _active_storage)
        # Helper calls fold their spend (budget clauses bound real
        # money) but don't count as an agent call and don't pollute
        # the per-task output average used by pre-call estimates.
        helper = _is_helper_call(ctx)
        enforcer.record_call(
            run_id=ctx.run_id,
            nanodollars=ctx.estimated_nanodollars,
            output_tokens=None if helper else output_tokens,
            task=task,
            count_call=not helper,
        )
    except Exception:  # pragma: no cover - defensive; never break a call
        _LOG.warning("contract record_call failed", exc_info=True)


# ----------------------------------------------------------------------
# Outcome window (advisory)
# ----------------------------------------------------------------------


def notify_outcome(run_id: str, outcome: str) -> None:
    """Evaluate the trailing outcome window for the run's task.

    Emits an advisory ``contract_violation`` event (``level=outcome``)
    when the recent success rate is below the contract floor. Never
    blocks and never raises into the caller.
    """
    enforcer = _active_enforcer
    storage = _active_storage
    if enforcer is None or storage is None:
        return
    try:
        task, tier = _facts_for_run(run_id, storage)
        if not enforcer.has_contract(task):
            return
        window_runs = _window_size(enforcer, task, tier)
        # Exclude the current run from the projection read and prepend its
        # live outcome unconditionally. The aggregator is a background
        # thread, so querying without this exclusion is timing-dependent:
        # if it has already projected the current run (row 0 by
        # started_at), it would be counted twice and one genuinely-older
        # run dropped by the window truncation.
        recent = _recent_outcomes(
            storage, task, window_runs, exclude_run_id=run_id
        )
        recent = [outcome] + recent
        violation = enforcer.evaluate_outcome_window(
            task=task, recent_outcomes=recent, tier=tier
        )
        if violation is not None:
            _emit_violations(
                run_id,
                EnforcementOutcome(action="allow", violations=(violation,)),
                storage,
            )
    except Exception:  # pragma: no cover - advisory path must never break a run
        _LOG.warning("contract outcome evaluation failed", exc_info=True)


def _window_size(
    enforcer: ContractEnforcer, task: Optional[str], tier: Optional[str]
) -> int:
    if task is None:
        return 100
    contract = enforcer._contracts.get(task)  # noqa: SLF001 - internal collaborator
    if contract is None:
        return 100
    clause = contract.resolved_outcome(tier)
    return clause.measure_window_runs if clause else 100


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _is_helper_call(ctx: "CallContext") -> bool:
    """True when ``ctx`` is a policy-internal helper call.

    CheapSummariser stamps its nested cheap-model call before this
    module sees it (policy ``before_call`` hooks run first in the
    shims), so the flag is reliably present here.
    """
    # Imported at call time: _emit imports policy modules that import
    # this module's siblings (same cycle _emit_violations avoids).
    from inkfoot.shims._emit import SUMMARISER_CALL_METADATA_KEY  # noqa: PLC0415

    return bool(ctx.metadata.get(SUMMARISER_CALL_METADATA_KEY))


def _facts_for_run(
    run_id: str, storage: Optional["Storage"]
) -> tuple[Optional[str], Optional[str]]:
    cached = _run_facts.get(run_id)
    if cached is not None:
        return cached
    task: Optional[str] = None
    tier: Optional[str] = None
    if storage is not None and hasattr(storage, "get_run"):
        try:
            row = storage.get_run(run_id)
        except Exception:  # pragma: no cover - defensive
            row = None
        if row:
            task = row.get("task")
            tier = _tier_from_metadata(_load_metadata(row.get("metadata_json")))
    with _lock:
        _run_facts[run_id] = (task, tier)
    return task, tier


def _load_metadata(raw: Any) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _tier_from_metadata(metadata: Optional[dict[str, Any]]) -> Optional[str]:
    if not metadata:
        return None
    tier = metadata.get("tenant_tier")
    return tier if isinstance(tier, str) and tier else None


def _emit_violations(
    run_id: str, outcome: EnforcementOutcome, storage: Optional["Storage"]
) -> None:
    if storage is None or not outcome.violations:
        return
    from ulid import ULID

    from inkfoot.shims._emit import _next_sequence, _now_ms

    for violation in outcome.violations:
        try:
            storage.insert_event(
                event_id=str(ULID()),
                run_id=run_id,
                kind="contract_violation",
                occurred_at=_now_ms(),
                sequence=_next_sequence(run_id),
                payload_json=json.dumps(violation.to_payload(), default=str),
                capture_mode="metadata",
            )
        except Exception:  # pragma: no cover - defensive
            _LOG.warning("failed to record contract_violation", exc_info=True)


def _recent_outcomes(
    storage: "Storage",
    task: Optional[str],
    limit: int,
    *,
    exclude_run_id: Optional[str] = None,
) -> list[str]:
    if task is None:
        return []
    try:
        conn = storage._conn()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - non-SQLite backend
        return []
    cur = conn.execute(
        """
        SELECT outcome FROM runs
        WHERE task = ? AND outcome IS NOT NULL AND id != ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [task, exclude_run_id or "", int(limit)],
    )
    return [row[0] for row in cur.fetchall() if row[0] is not None]


def _block_message(task: Optional[str], violation: Any) -> str:
    if violation is None:
        return (
            f"Token Contract for task {task!r} blocked this call: the run "
            f"reached its budget ceiling."
        )
    return (
        f"Token Contract for task {task!r} blocked this call: "
        f"{violation.clause_name} projected to {violation.projected_value:g} "
        f"against a ceiling of {violation.threshold:g}."
    )
