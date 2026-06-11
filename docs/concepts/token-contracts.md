# Token Contracts

A **Token Contract** is a small YAML file that states the budget and
quality a task is expected to hold to. Contracts are *code*: they live
in your repository, are reviewed in pull requests, and are
version-controlled alongside the agent they govern. Unlike
[observation policies](observation-policies.md) — which only warn —
a contract can actively reshape or refuse a call as a run approaches
its ceiling, and the same file doubles as a CI gate that fails a build
before a budget regression ships.

## The shape of a contract

```yaml
schema_version: 1
task: customer-support-triage
cheap_model: claude-haiku-4-5
budget:
  max_nanodollars: 50_000_000      # $0.05 per run
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
```

Every field under `budget` and `outcome` is optional — state only the
limits you care about. Unknown keys are rejected at load time, so a
misspelled `max_nanodolars` fails loudly instead of silently disabling
a budget.

## The degrade ladder

The `degrade` list is the heart of a contract. Each rung pairs a
budget percentage with an action the runtime takes once a run's
*projected* spend reaches that percentage of the ceiling:

- **`warn`** — record a `contract_violation` event; the call proceeds
  unchanged.
- **`switch_to_cheap_model`** — rewrite the call's model to the
  contract's `cheap_model` and let it proceed on the cheaper model. A
  contract that uses this action must declare a `cheap_model`.
- **`block`** — refuse the call. The underlying SDK request is never
  made and `inkfoot.errors.PolicyBlocked` is raised to your code.

Rungs are evaluated against whichever budget dimension is most
consumed (cost or call count), so a run that is cheap but chatty trips
the ladder on `max_llm_calls` just as readily as an expensive one trips
it on `max_nanodollars`. Each rung emits its event once per run; a
`block` keeps refusing every subsequent call.

Cost estimation happens *before* each call, so it has to predict the
output token count that hasn't been generated yet. The estimator is
pessimistic by design — it uses a per-task moving average of recent
output sizes (defaulting to 500 tokens) and rounds up — and the ladder
fires at coarse percentages precisely so a noisy estimate still catches
a runaway run before it blows the ceiling.

### Policy helper calls don't count

Modification policies sometimes make LLM calls of their own —
[`CheapSummariser`](modification-policies.md#cheapsummariserthreshold_tokens1500)
calls a cheap model to condense an oversized tool result. Those
helper calls are exempt from enforcement: they are never warned on,
model-switched, or blocked, and they don't advance `max_llm_calls`
or the per-task output average behind the pre-call estimate. Gating
them would silently degrade the policy exactly when the contract is
tightest — and the policy exists to *reduce* spend. The helper's
real cost still folds into the run's running spend, so
`max_nanodollars` keeps bounding actual money.

## Enabling enforcement

Pass your contracts to `inkfoot.instrument`:

```python
import inkfoot

inkfoot.instrument(contracts=["./contracts"])
```

You can pass directories, individual files, or a mix. The task on each
contract is matched against the `task` you give to
`inkfoot.agent_run(task=...)`; a run whose task has no contract is left
completely untouched.

```python
with inkfoot.agent_run(task="customer-support-triage"):
    response = client.messages.create(...)  # governed by the contract
```

### Per-tenant overrides

The `overrides` block layers tier-specific limits over the base
clauses. The tier is read from the run's `metadata["tenant_tier"]`:

```python
with inkfoot.agent_run(
    task="customer-support-triage",
    metadata={"tenant_tier": "free_tier"},
):
    ...
```

A free-tier run in the example above enforces a $0.01 ceiling while
keeping every other base-clause limit.

## Outcome clauses are advisory

The `outcome` block measures a task's success rate across a trailing
window of runs. When the recent rate falls below
`required_success_rate`, a `contract_violation` event with
`level="outcome"` is emitted — but it **never blocks a call and never
fails a build**. A benchmark scenario can't stand in for production
outcome quality, so gating on it would either rubber-stamp every run or
punish unrelated drift. Treat outcome clauses as a signal to
investigate, not a tripwire.

## Drafting a contract from history

If you already have runs recorded but no contract yet, let inkfoot
propose one from the observed spread:

```bash
inkfoot contract draft --task customer-support-triage --window 30d --output contracts/triage.yaml
```

The draft sizes the budget a little above your real usage (p95 cost +
10% headroom, p99 calls + 1) and flags cost outliers in a header
comment instead of letting one pathological run inflate the numbers.
It's a starting point — read it, adjust it, and commit it.

## Checking contracts in CI

The same contract that governs runtime also gates your pull requests.
Run a benchmark to produce `current.json`, then evaluate your contracts
against it:

```bash
inkfoot contract check ./contracts --against current.json
```

The command exits `0` when every budget clause is comfortably within
its ceiling, `1` for a soft warning (a clause within 10% of its
ceiling), and `2` for a violation. To fold the verdict into the same
sticky PR comment as your [cost diff](../reference/cli.md#inkfoot-diff),
pass `--contracts` to `inkfoot diff`:

```bash
inkfoot diff baseline.json current.json --contracts ./contracts
```

See the [CLI reference](../reference/cli.md#inkfoot-contract) for the
full flag list, and the
[schema changelog](../reference/contract-schema-changelog.md) for the
versioning and deprecation policy.

## Schema versioning

Every contract declares a `schema_version`. The loader accepts the
current version and the one immediately before it (loaded with a
one-time deprecation warning); anything older is rejected with an
actionable "migrate your contracts" message. The
[schema changelog](../reference/contract-schema-changelog.md) records
what changed between versions and how to migrate.
