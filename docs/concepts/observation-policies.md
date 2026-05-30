# Observation Policies

Policies are objects you pass to `inkfoot.instrument(policies=[...])` to
get live-running warnings on a run while it executes. They run inside
the SDK shim's call wrapper, so they see every call as it happens —
unlike smells, which only evaluate when you render a report.

Inkfoot ships three observation policies:

- `BudgetCap` — warn when a run's cumulative cost crosses a threshold.
- `RetryThrottle` — warn when a run retries too many times in a window.
- `CacheControlPlacer` — advise on missing `cache_control` markers
  (Anthropic only).

All three are **observe-only**: they emit events into the database when
their trigger fires, but they never block or rewrite the underlying SDK
call. Reports surface the events alongside the bar chart.

## Registering policies

```python
import inkfoot
from inkfoot.policy import BudgetCap, RetryThrottle, CacheControlPlacer

inkfoot.instrument(policies=[
    BudgetCap(max_nd=50_000_000),       # $0.05 per run
    RetryThrottle(window_s=60, max=3),  # ≤3 retries per 60s
    CacheControlPlacer(),
])
```

Policies are validated against the active integration at registration
time. Passing a policy that doesn't support the active integration
raises `inkfoot.errors.PolicyNotSupported` with a remediation hint
pointing at the docs URL for that policy.

## `BudgetCap(max_nd)`

Emits a `budget_warning` event when a run's cumulative estimated cost
crosses `max_nd` nanodollars.

```python
BudgetCap(max_nd=10_000_000)   # $0.01
BudgetCap(max_nd=1_000_000_000)  # $1.00
```

**Argument.**

- `max_nd` — integer nanodollars (10⁻⁹ USD per unit). Must be
  non-negative. Use `inkfoot.money.usd_to_nd(Decimal("0.05"))` for a
  Decimal-friendly value.

**Behaviour.**

- The policy keeps a running total per `run_id`.
- When the total crosses `max_nd`, the *next* LLM call's `before_call`
  hook emits a `budget_warning` event. (The hook runs before it can
  know the current call's cost, so the warning lags by one call.)
- Fires once per run — subsequent calls in the same run that would
  also breach do not re-fire.
- **Never blocks the call.** Inkfoot is currently observe-only;
  enforcement is a future capability.

**Event payload includes** `current_total_nd` and `max_nd`.

## `RetryThrottle(window_s, max)`

Emits a `retry_throttle` event when the count of observed retries in a
rolling window exceeds the threshold.

```python
RetryThrottle(window_s=60, max=3)   # >3 retries in any 60s window
```

**Arguments.**

- `window_s` — rolling window in wall-clock seconds. Must be positive.
- `max` — retry count threshold. Must be ≥ 1.

**Behaviour.**

- Retries are *not* detected automatically — the policy looks for a
  `retry` flag in the call's metadata, which your code sets via
  `inkfoot.tag("retry", True)` or via a framework adapter that knows
  about retries.
- Fires on the breach call — i.e. when the (max+1)th retry lands inside
  the window.
- After firing, the policy continues to track but won't fire again for
  the same run until the count drops back below the threshold.
- Optionally, set `inkfoot.tag("retry_cause", "ratelimit")` (any string)
  so the per-cause retry counts get tracked in the in-memory run state
  for downstream reporting.

**Event payload includes** `retry_count`, `window_s`, and `max`.

## `CacheControlPlacer()`

Anthropic-only. Inspects each Anthropic request and emits a
`cache_control_advice` event when the system block or tools array is
large enough to benefit from a `cache_control` marker but doesn't have
one.

**Behaviour.**

- OpenAI calls are silently ignored (OpenAI's prompt caching is
  automatic and doesn't use client markers).
- Fires at most once per block per run — once the policy has advised on
  the system block for a given run, it won't repeat the system advice
  for that run.
- The minimum block size before advice fires is roughly 4096 characters
  (Anthropic only caches blocks of at least 1024 tokens; the threshold
  approximates that bound).
- This policy is **advice-only** in the current release: it tells you
  where the markers should go, but it does not rewrite your request.
  The event metadata includes the proposed marker placement so a future
  modification policy can act on it automatically.

**Event payload includes** `blocks` (list of block names like
`["system", "tools"]`) and `proposed_markers` (the marker shapes
suggested for each block).

## Surface in reports

Policy events show up in the events table with `kind='budget_warning'`,
`'retry_throttle'`, or `'cache_control_advice'`. Future report views
will surface them inline; for now, you can query them directly:

```bash
sqlite3 ~/.inkfoot/runs.db \
  "SELECT kind, payload_json FROM events
   WHERE run_id = 'run-01JZX...' AND kind LIKE '%warning%'"
```

## Writing your own policy

Policies are simple — inherit from `inkfoot.policy.Policy` and implement
the two hooks:

```python
from inkfoot.policy import Policy, PolicyDecision, IntegrationPattern

class MyPolicy(Policy):
    NAME = "MyPolicy"
    SUPPORTED_PATTERNS = {IntegrationPattern.A}

    def before_call(self, ctx):
        # Decide whether to emit an event before the SDK call runs.
        if some_condition(ctx):
            return PolicyDecision(
                action="warn",
                reason="...",
                emit_event_kind="my_warning",
                metadata={"k": "v"},
            )
        return PolicyDecision(action="allow")

    def after_call(self, ctx, response):
        # Update per-policy state. Cannot return a decision.
        ...
```

Both hooks are wrapped in Inkfoot's exception-isolation decorator at
the shim boundary. A policy that raises is logged at `WARNING` and the
user's LLM call always returns the original SDK response — your bug
will never break a customer's agent.

Register your policy by passing it in the `policies` list of
`inkfoot.instrument()`.
