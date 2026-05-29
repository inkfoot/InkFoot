"""Scenario execution + aggregation for ``inkfoot benchmark``
(Phase 1 / E2-S1 / phase-1-explain §4.3).

Responsibilities:

1. Discover scenarios under a directory (delegates to
   :class:`inkfoot.benchmark.scenario.ScenarioLoader`).
2. For each ``scenario × fixture × runs_per_fixture``, open an
   ``agent_run`` block tagged with the scenario's ``task`` and
   invoke ``scenario.run(fixture)``.
3. Aggregate per-scenario stats from the storage event log into a
   :class:`inkfoot.benchmark.schema.BenchmarkArtifact`.

ADR-1-4 is decisive: benchmark scenarios make *live* LLM calls.
That makes determinism a non-goal at the cost level — the runner's
job is to faithfully capture what each run cost and aggregate it.
Storage is opened against a tempfile by default so a CI run is
isolated from any pre-existing local DB.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from inkfoot._run_lifecycle import agent_run, set_outcome
from inkfoot._version import __version__
from inkfoot.benchmark.scenario import (
    Scenario,
    ScenarioLoadError,
    ScenarioLoader,
)
from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    ScenarioResult,
    SmellCount,
    percentile,
)


_LOG = logging.getLogger("inkfoot.benchmark.runner")


# Helper type aliases used by the runner's internal aggregation.
_RunStats = dict[str, Any]


@contextmanager
def _ephemeral_storage(db_path: Optional[Path] = None) -> Iterator[Any]:
    """Yield a connected storage instance for the benchmark run.

    A tempfile is used by default so multiple benchmark invocations
    in the same shell don't accumulate into a single DB and so a CI
    runner doesn't poison the developer's local DB. Tests pass an
    explicit path to assert on the event stream after the fact.
    """
    from inkfoot.storage.sqlite import SQLiteStorage  # noqa: PLC0415

    cleanup: Optional[Path] = None
    if db_path is None:
        tmp = Path(tempfile.mkdtemp(prefix="inkfoot-bench-"))
        db_path = tmp / "runs.db"
        cleanup = tmp
    storage = SQLiteStorage(path=db_path)
    storage.connect()
    try:
        yield storage
    finally:
        storage.close()
        if cleanup is not None:
            # Best-effort cleanup; the tempdir lives under /tmp so
            # leaving it isn't catastrophic if the unlink fails.
            for child in sorted(cleanup.glob("*"), reverse=True):
                try:
                    child.unlink()
                except OSError:  # pragma: no cover — defensive
                    _LOG.debug("could not remove %s", child)
            try:
                cleanup.rmdir()
            except OSError:  # pragma: no cover — defensive
                _LOG.debug("could not rmdir %s", cleanup)


def run_benchmark(
    scenarios_dir: Path | str,
    *,
    output: Optional[Path | str] = None,
    scenarios_only: Optional[Iterable[str]] = None,
    instrument: Optional[Callable[..., Any]] = None,
    loader: Optional[ScenarioLoader] = None,
    storage_path: Optional[Path] = None,
) -> BenchmarkArtifact:
    """Execute every discovered scenario and return the aggregated
    artefact. Optionally writes it to ``output``.

    Args:
        scenarios_dir: Directory to walk for ``.py`` scenario files.
        output: When provided, the artefact is also written to disk
            at this path. The CLI passes ``--output`` here.
        scenarios_only: Optional iterable of scenario task names; only
            matching scenarios are run. Mirrors the spec's
            ``--scenarios-only NAME`` flag.
        instrument: Function called once to boot Inkfoot's
            instrumentation against the runner's storage. Defaults to
            :func:`inkfoot.instrument`; tests inject a stub so they
            don't have to drive a real SDK.
        loader: Custom scenario loader (tests use this for in-memory
            fixture loading).
        storage_path: When set, runs use this SQLite path instead of
            an ephemeral tempfile. Mostly for tests.

    Returns:
        The :class:`BenchmarkArtifact` describing per-scenario stats.
    """
    from inkfoot._instrument import (  # noqa: PLC0415
        instrument as default_instrument,
        shutdown as default_shutdown,
    )

    boot = instrument or default_instrument
    loader = loader or ScenarioLoader()

    scenarios = loader.discover(Path(scenarios_dir))
    if scenarios_only:
        wanted = {s.strip() for s in scenarios_only if s and s.strip()}
        scenarios = [s for s in scenarios if s.task in wanted or s.name in wanted]
    if not scenarios:
        # Empty discovery is *not* a hard error: a fresh scenarios/
        # dir with only conftest.py should produce an empty artefact
        # so the CI step can still emit a "no scenarios ran" diff.
        _LOG.warning("benchmark: no scenarios discovered in %s", scenarios_dir)

    # ``instrument()`` is idempotent, so a prior call in the same
    # process leaves the previous storage attached. Shut down first
    # so each benchmark invocation gets a clean slate against our
    # ephemeral DB; tests rely on this for back-to-back runs.
    try:
        default_shutdown()
    except Exception:  # pragma: no cover — defensive
        _LOG.debug("pre-benchmark shutdown raised", exc_info=True)

    with _ephemeral_storage(storage_path) as storage:
        # Boot Inkfoot against this storage explicitly so scenarios
        # don't fall back to the user's default DB on disk.
        boot(storage=storage)
        try:
            scenario_results: list[ScenarioResult] = []
            for scenario in scenarios:
                stats = _execute_scenario(scenario, loader, storage)
                scenario_results.append(_aggregate_scenario(scenario, stats))
        finally:
            # Tear down the instrumentation before we close the
            # storage so the aggregator drains its dirty queue and
            # no background thread keeps a stale storage handle.
            try:
                default_shutdown()
            except Exception:  # pragma: no cover — defensive
                _LOG.debug("post-benchmark shutdown raised", exc_info=True)

    captured_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    artefact = BenchmarkArtifact(
        inkfoot_version=__version__,
        schema_version=BENCHMARK_SCHEMA_VERSION,
        captured_at=captured_at,
        scenarios=tuple(scenario_results),
    )
    if output is not None:
        artefact.write(Path(output))
    return artefact


def _execute_scenario(
    scenario: Scenario,
    loader: ScenarioLoader,
    storage: Any,
) -> list[_RunStats]:
    """Run ``scenario`` against every fixture × ``runs_per_fixture``.

    Returns one stats dict per executed run. The dict carries the
    fields the aggregator needs (cost, cache breakdown, smell hits)
    so the artefact builder doesn't need to re-query storage rows
    individually.
    """
    runs: list[_RunStats] = []
    fixtures = list(loader.iter_fixture_payloads(scenario))
    if not fixtures:
        return runs
    for fixture_path, payload in fixtures:
        for attempt in range(scenario.runs_per_fixture):
            run_stats = _execute_one(scenario, fixture_path, payload, storage)
            run_stats["attempt"] = attempt
            runs.append(run_stats)
    return runs


def _execute_one(
    scenario: Scenario,
    fixture_path: str,
    payload: Any,
    storage: Any,
) -> _RunStats:
    """Drive a single ``scenario.run(fixture)`` invocation.

    Wraps it in an ``agent_run`` block so the events land in
    storage; records the run id, success flag, and post-hoc the
    aggregated ledger / smell hits.

    Outcome resolution (Finding #7): we honour any outcome the
    scenario emits via ``inkfoot.set_outcome(...)``. If the
    scenario doesn't set one and ``run()`` returned normally, we
    fall back to the scenario's declared ``expected_outcome``. On
    exception, we always record ``"failure"``. The scenario's
    ``successes`` count is then the number of runs whose recorded
    outcome equals ``expected_outcome``.
    """
    raised = False
    error: Optional[str] = None
    with agent_run(
        task=scenario.task,
        metadata={"fixture": fixture_path, "benchmark": True},
    ) as handle:
        run_id = handle.id
        try:
            scenario.run(payload)
        except Exception as exc:  # pylint: disable=broad-except
            # Capture the failure mode rather than aborting the
            # benchmark; one bad fixture should not erase the rest of
            # the run's signal.
            raised = True
            error = f"{type(exc).__name__}: {exc}"
            _LOG.warning(
                "scenario %s fixture %s raised: %s\n%s",
                scenario.task,
                fixture_path,
                error,
                traceback.format_exc(),
            )
        finally:
            try:
                _ensure_outcome(
                    storage=storage,
                    run_id=run_id,
                    raised=raised,
                    expected_outcome=scenario.expected_outcome,
                )
            except Exception:  # pragma: no cover — defensive
                _LOG.debug("ensure_outcome failed for %s", run_id)

    return _summarise_run(
        storage, run_id, scenario.expected_outcome, error
    )


def _ensure_outcome(
    *,
    storage: Any,
    run_id: Optional[str],
    raised: bool,
    expected_outcome: str,
) -> None:
    """Emit a default outcome only if the scenario didn't set one.

    Peeks at the run's event stream for an existing ``outcome``
    event; if none, falls back to ``"failure"`` (on exception) or
    the scenario's declared ``expected_outcome`` (on clean return).
    The fall-through path keeps the spec intent — a scenario that
    declares ``expected_outcome="human_escalated"`` and returns
    normally is *not* implicitly demoted to ``"success"``.
    """
    if run_id is None:
        return
    if not raised:
        for ev in storage.iter_events(run_id):
            if isinstance(ev, dict) and ev.get("kind") == "outcome":
                return  # scenario already declared its own outcome
        set_outcome(expected_outcome)
    else:
        set_outcome("failure")


def _summarise_run(
    storage: Any,
    run_id: Optional[str],
    expected_outcome: str,
    error: Optional[str],
) -> _RunStats:
    """Read the run's events from storage and produce a flat summary.

    The runner aggregates only what the schema needs:

    * ``nanodollars``: estimated total cost (per
      :func:`inkfoot.pricing.estimate_nanodollars`),
    * ``llm_calls``: count of ``llm_call`` events,
    * ``cache_read_tokens`` / ``cache_creation_tokens``: used by the
      cache-hit-rate formula in :func:`_aggregate_scenario`,
    * ``succeeded``: whether the run's *recorded* outcome matches
      the scenario's ``expected_outcome`` (Finding #7).

    The full event stream stays in storage for the user to drill
    into with ``inkfoot report`` after the fact."""
    if run_id is None:
        # ``agent_run`` failed to start (storage outage etc.). Emit
        # a zero-stats record so the scenario isn't silently dropped.
        return {
            "run_id": None,
            "succeeded": False,
            "nanodollars": 0,
            "llm_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "input_total_tokens": 0,
            "smell_ids": (),
            "error": error,
        }

    events = list(storage.iter_events(run_id))
    recorded_outcome = _last_outcome(events)
    succeeded = recorded_outcome == expected_outcome
    nanodollars = 0
    llm_calls = 0
    cache_read = 0
    cache_creation = 0
    input_total = 0
    for ev in events:
        if ev.get("kind") != "llm_call":
            continue
        raw = ev.get("payload_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        llm_calls += 1
        nanodollars += int(
            payload.get("estimated_nanodollars")
            or payload.get("estimated_cost_nanodollars")
            or 0
        )
        # Cache fields live at the top-level NeutralCall dict; an
        # adapter that hasn't populated them leaves them at zero,
        # which the rate formula tolerates.
        cache_read += int(payload.get("cache_read_tokens", 0) or 0)
        cache_creation += int(payload.get("cache_creation_tokens", 0) or 0)
        # The 11 structural input categories. We hand-roll the sum
        # rather than importing INPUT_CATEGORIES into this hot path
        # to keep the runner module standalone-importable.
        for key in (
            "system_static_tokens",
            "system_dynamic_tokens",
            "user_input_tokens",
            "tool_schema_tokens",
            "tool_result_tokens",
            "retrieved_context_tokens",
            "memory_tokens",
            "retry_overhead_tokens",
            "summariser_tokens",
            "reasoning_tokens",
            "guardrail_tokens",
        ):
            input_total += int(payload.get(key, 0) or 0)

    smell_ids = _detect_smells(storage, run_id, events)
    return {
        "run_id": run_id,
        "succeeded": succeeded,
        "nanodollars": nanodollars,
        "llm_calls": llm_calls,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "input_total_tokens": input_total,
        "smell_ids": smell_ids,
        "error": error,
    }


def _last_outcome(events: list[dict[str, Any]]) -> Optional[str]:
    """Return the most recent ``outcome`` event's ``outcome`` field.

    Multiple ``set_outcome`` calls per run are unusual but allowed
    (the last one wins). We treat them in sequence order so a
    later, scenario-emitted ``human_escalated`` overrides any
    earlier ``success`` placeholder a fixture might have set.
    """
    last: Optional[str] = None
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != "outcome":
            continue
        raw = ev.get("payload_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if isinstance(outcome, str):
            last = outcome
    return last


def _detect_smells(
    storage: Any, run_id: str, events: list[dict[str, Any]]
) -> tuple[str, ...]:
    """Return the set of smell ids that fired on this run.

    Smell evaluation here matches what ``inkfoot report`` would show
    for the run, so users can drill into a regressed benchmark and
    see the same finding the diff surfaced."""
    from inkfoot.smells import DEFAULT_SMELLS  # noqa: PLC0415
    from inkfoot.smells.engine import SmellEngine  # noqa: PLC0415

    row = storage.get_run(run_id) if hasattr(storage, "get_run") else None
    if row is None:
        return ()
    engine = SmellEngine(list(DEFAULT_SMELLS))
    findings = engine.evaluate(row, events)
    return tuple(sorted({hit.smell.id for hit in findings}))


def _aggregate_scenario(
    scenario: Scenario, stats: list[_RunStats]
) -> ScenarioResult:
    """Roll up per-run stats into a single :class:`ScenarioResult`.

    Cache-hit rate is ``cache_read / (cache_read + cache_creation +
    input_total)`` — cached reads divided by the total billed input
    "the agent saw". This rewards prefix reuse and penalises new
    cache writes, which is the signal CI cost review cares about.
    """
    runs = len(stats)
    if runs == 0:
        return ScenarioResult(
            task=scenario.task,
            runs=0,
            successes=0,
            p50_nanodollars=0,
            p95_nanodollars=0,
            mean_llm_calls=0.0,
            mean_cache_hit_rate=0.0,
            smells_seen=(),
        )

    successes = sum(1 for s in stats if s.get("succeeded"))
    costs = [int(s.get("nanodollars", 0) or 0) for s in stats]
    calls = [int(s.get("llm_calls", 0) or 0) for s in stats]
    p50 = int(percentile(costs, 50))
    p95 = int(percentile(costs, 95))

    smell_counts: dict[str, int] = {}
    for s in stats:
        for smell_id in s.get("smell_ids", ()):  # type: ignore[union-attr]
            smell_counts[smell_id] = smell_counts.get(smell_id, 0) + 1

    # Aggregation choice (Finding #9): we use a mean-of-per-run-rates
    # rather than a token-weighted ratio-of-sums so a single
    # high-cost outlier run with a cache miss doesn't drown out the
    # other runs' signal. Trade-off: scenarios with extremely
    # heterogeneous fixture sizes get equal weight per run, not per
    # token. Document the choice here so a future reader doesn't
    # "fix" it.
    cache_rates: list[float] = []
    for s in stats:
        cache_read = int(s.get("cache_read_tokens", 0) or 0)
        cache_creation = int(s.get("cache_creation_tokens", 0) or 0)
        input_tokens = int(s.get("input_total_tokens", 0) or 0)
        denom = cache_read + cache_creation + input_tokens
        if denom > 0:
            cache_rates.append(cache_read / denom)
        else:
            # Zero denominator means the run didn't make a measurable
            # LLM call; exclude it rather than skewing the mean to 0.
            continue
    mean_cache_hit_rate = sum(cache_rates) / len(cache_rates) if cache_rates else 0.0

    return ScenarioResult(
        task=scenario.task,
        runs=runs,
        successes=successes,
        p50_nanodollars=p50,
        p95_nanodollars=p95,
        mean_llm_calls=sum(calls) / runs if runs else 0.0,
        mean_cache_hit_rate=mean_cache_hit_rate,
        smells_seen=tuple(
            SmellCount(id=sid, count=cnt) for sid, cnt in sorted(smell_counts.items())
        ),
    )


__all__ = [
    "run_benchmark",
    "ScenarioLoadError",
]
