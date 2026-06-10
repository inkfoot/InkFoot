# CLI Reference

The `inkfoot` command is installed by `pip install inkfoot`. It exposes
the following subcommands:

| Command | Purpose |
|---|---|
| [`inkfoot report`](#inkfoot-report) | Render a single-run attribution + smells, or aggregate across recent runs. |
| [`inkfoot tag`](#inkfoot-tag) | Attach a tag to a run after it has finished. |
| [`inkfoot rebuild-aggregates`](#inkfoot-rebuild-aggregates) | Recompute run totals from the event log. |
| [`inkfoot benchmark`](#inkfoot-benchmark) | Run scenario suites and emit a benchmark JSON artefact. |
| [`inkfoot diff`](#inkfoot-diff) | Compare two benchmark JSONs and emit a Markdown or JSON report. |
| [`inkfoot tail`](#inkfoot-tail) | Stream events live as the agent runs. |

The `--db <path>` flag is accepted on commands that read or write
the SQLite event log: `report`, `tag`, `tail`, and
`rebuild-aggregates`. The default location is
`~/.inkfoot/runs.db`; alternatively set `INKFOOT_HOME=<dir>` to
relocate the parent directory (both the agent and the CLI must
see the same value).

`benchmark` runs against an ephemeral, tempdir-scoped database
created per invocation, and `diff` reads JSON artefacts only, so
neither accepts `--db`.

## `inkfoot report`

Render the attribution bar chart + detected smells for one run, or a
summary table across many runs.

### Single-run view

```bash
inkfoot report --run <run-id>
```

| Flag | Purpose |
|---|---|
| `--run <id>` | Render the run with this ULID. |
| `--show-zero` | Show all fourteen ledger fields including always-zero rows. |
| `--no-smells` | Skip smell evaluation and hide the smells stanza. Useful when you only want the attribution chart. |
| `--db <path>` | Override the default database path. |

The smell engine runs by default on every single-run view, so
the `Smells detected` block below appears any time at least one
smell fires. Pass `--no-smells` to suppress it.

Output:

```
Run run-01JZX0... · customer-support-triage · 4.2s · $0.0123 · success (0.95)

Causal attribution:
  system_static       42.1%  ██████░░░░░░  $0.0052
  user_input          28.4%  ███░░░░░░░░░  $0.0035
  tool_result         14.7%  █░░░░░░░░░░░  $0.0018  ⚠ oversized
  output              10.3%  █░░░░░░░░░░░  $0.0013
  cache_read           4.5%  ░░░░░░░░░░░░  $0.0005

(summariser, guardrail, and retry_overhead are always-zero — hidden by default)

Smells detected (1):
  · oversized-tool-result-recycled  (oversized)
    → Summarise large tool results before recycling them across turns.

Estimated savings if fixed: ~$0.0042/run (-34%).
```

The header line includes: run ID · task · duration · cost · outcome
(with quality score). Zero-cost categories are hidden unless
`--show-zero` is passed; the footnote names which always-zero
categories were hidden.

If a smell anchors to one of the bar-chart rows (its
`primary_category`), a `⚠ <short-name>` marker appears beside that row.

### Aggregate view

```bash
inkfoot report --last 7d --group-by task
```

| Flag | Purpose |
|---|---|
| `--last <duration>` | Time window. Format: `<n><unit>` where unit is `s`, `m`, `h`, or `d`. Examples: `24h`, `7d`, `30d`. |
| `--group-by <field>` | Bucket the table by `task` (default) or `agent_kind`. |
| `--task <name>` | Filter to runs with this `task` value. |
| `--no-smells` | Skip the cross-run smell rollup at the bottom of the view. |
| `--db <path>` | Override the default database path. |

Output:

```
Recent runs (7d, grouped by task):

  bucket                              runs      avg_$      p95_$  success%   cost/success
  customer-support-triage              142   $0.0118    $0.0341      94.3%        $0.0125
  invoice-extraction                    37   $0.0432    $0.0982      89.2%        $0.0484
  meeting-summariser                    21   $0.0067    $0.0145      95.2%        $0.0070

Aggregate smells (last 7d):
  · unstable-prompt-prefix: 56/200 runs (28%)
  · oversized-tool-result-recycled: 12/200 runs (6%)
```

The trailing `Aggregate smells` stanza counts the number of runs
in the window that fired each smell at least once. The rollup
scans up to 500 of the most recent matching runs; tighten the
`--last` window if you need a precise rate on a larger
population. Pass `--no-smells` to omit the stanza.

| Column | Meaning |
|---|---|
| `bucket` | The value of the grouping column. `(none)` if null. |
| `runs` | Run count in this bucket. |
| `avg_$` | Average `total_nanodollars` per run. |
| `p95_$` | 95th-percentile per-run cost. |
| `success%` | Percentage of runs whose `outcome` is `"success"`. |
| `cost/success` | Total cost ÷ number of successful runs (or `—` when no successes). |

## `inkfoot tag`

Attach a `(key, value)` tag to an existing run. Use this when you
forgot to call `inkfoot.tag(...)` at runtime, or when an out-of-band
process (e.g. a quality reviewer) needs to annotate a finished run.

```bash
inkfoot tag <run-id> <key> <value>
```

| Argument | Purpose |
|---|---|
| `run-id` | The ULID of the run to tag. |
| `key` | The tag key (free-form string). |
| `value` | The tag value. Parsed as JSON when possible — `5` becomes an integer, `true` becomes a boolean, `"text"` becomes a string. Unparseable values are stored as raw strings. |
| `--db <path>` | Override the default database path. |

Example:

```bash
inkfoot tag run-01JZX0... reviewed_by alice
inkfoot tag run-01JZX0... quality_score 0.87
```

The tag is recorded as a `user_tag` event on the run. After the
background aggregator picks it up (usually within a second), it shows up
in `inkfoot report --run <run-id>`.

## `inkfoot rebuild-aggregates`

Mark every run dirty and force the background aggregator to recompute
`runs.total_*` columns from the event log.

```bash
inkfoot rebuild-aggregates
```

| Flag | Purpose |
|---|---|
| `--db <path>` | Override the default database path. |

When to use it:

- After a `kill -9` or unclean shutdown — the event log is intact but a
  run's totals may be stale.
- After manually editing the database — to put the projection back in
  sync.
- After a Inkfoot upgrade that adds a new projection column.

The command prints how many runs it marked dirty and how many it
successfully re-projected:

```
rebuild-aggregates: marked 4271 runs dirty; drained 4271 runs.
```

The event log is the source of truth — the projection is always
recomputable from it, so `rebuild-aggregates` is always safe to run.

## `inkfoot benchmark`

Run every `.py` scenario in a directory under instrumentation and emit
a benchmark JSON artefact. Each scenario file must
export an `INKFOOT_SCENARIO` dict and a `run(fixture)` callable:

```python
# tests/agent_scenarios/customer_support_triage.py
INKFOOT_SCENARIO = {
    "task": "customer-support-triage",
    "fixtures": ["fixtures/ticket-1.json", "fixtures/ticket-2.json"],
    "expected_outcome": "success",
    "runs_per_fixture": 1,
}

def run(fixture: dict) -> dict:
    return my_agent.handle(fixture)
```

```bash
inkfoot benchmark tests/agent_scenarios --output current.json
```

| Flag | Purpose |
|---|---|
| `--output PATH` | Persist the artefact JSON to `PATH` (in addition to stdout). |
| `--scenarios-only NAME` | Only run scenarios matching this name, checked against either the scenario's `INKFOOT_SCENARIO['task']` value or the bare filename stem (e.g. `triage` matches `triage.py`). Pass multiple times to whitelist several. |
| `--quiet` | Suppress the stdout JSON; rely on `--output`. |

The artefact shape (schema version 1):

```json
{
  "inkfoot_version": "1.0.0",
  "schema_version": "1",
  "captured_at": "2026-05-25T12:00:00Z",
  "scenarios": [
    {
      "task": "customer-support-triage",
      "runs": 8,
      "successes": 8,
      "p50_nanodollars": 41000000,
      "p95_nanodollars": 83000000,
      "mean_llm_calls": 4.1,
      "mean_cache_hit_rate": 0.82,
      "smells_seen": [
        {"id": "unstable-prompt-prefix", "count": 0},
        {"id": "oversized-tool-result-recycled", "count": 0}
      ]
    }
  ]
}
```

the benchmark makes **real** LLM calls — that's what
makes the resulting cost numbers meaningful. CI consumers should
path-filter their workflow to PRs that touch agent code and keep the
fixture set small (3–5 per scenario) to bound the cost-per-PR.

## `inkfoot diff`

Compare two benchmark artefacts and emit a structured report:

```bash
inkfoot diff baseline.json current.json --format markdown
```

| Flag | Purpose |
|---|---|
| `--format {markdown,json}` | Output format. `markdown` (default) is the PR-comment shape; `json` mirrors the benchmark artefact with a `delta` block per scenario. |
| `--thresholds {tight,default,loose,PATH}` | Verdict preset, or a path to a JSON file. Defaults match the documented diff contract (cost +20% warn, +50% fail; cache-hit -10% warn, -25% fail). |
| `--output PATH` | Also write the rendered report to `PATH`. |

Exit codes follow the verdict ladder:

| Verdict | Threshold breached | Exit code |
|---|---|---|
| `ok` | none | 0 |
| `warn` | soft threshold (cost / cache / outcome) or non-critical new smell | 1 |
| `fail` | hard threshold OR a critical smell appeared | 2 |

The Markdown renderer embeds a hidden HTML marker
(`<!-- inkfoot-diff-action -->`) so the GitHub Action can identify
and update its own PR comment on subsequent pushes.

See [CI cost review recipes](#ci-cost-review-recipes) below for
GitHub / GitLab / Bitbucket wiring.

## `inkfoot contract`

Work with [Token Contracts](../concepts/token-contracts.md) — the
version-controlled budget and outcome files that govern a task. Two
subcommands: `draft` generates a starting-point contract from run
history, `check` evaluates contracts against a benchmark artefact in CI.

### `inkfoot contract draft`

Read the runs recorded for a task and emit a contract YAML sized just
above the observed spread:

```bash
inkfoot contract draft --task customer-support-triage --window 30d --output contracts/triage.yaml
```

| Flag | Purpose |
|---|---|
| `--task <name>` | Task to draft a contract for. Required. |
| `--window <duration>` | History window to learn from (`30d`, `24h`, `90m`). Default `30d`. |
| `--output PATH` | Write the YAML here instead of stdout. |
| `--db <path>` | Override the default database path. |

The draft sets `max_nanodollars` to p95 cost + 10% headroom,
`max_llm_calls` to p99 + 1, `cache_hit_rate_min` to the p25 hit rate,
and `required_success_rate` to the observed rate minus a 1pp tolerance.
Cost outliers above 10× the median are listed in a header comment and
excluded from the percentiles rather than silently inflating the
budget. A window with fewer than 20 runs still produces a draft, but
prepends a warning that the numbers are a placeholder. The draft is a
starting point — review it, adjust, and commit it like any other code.

### `inkfoot contract check`

Evaluate a directory of contracts against a benchmark artefact — the CI
gate:

```bash
inkfoot contract check ./contracts --against current.json --format markdown
```

| Flag | Purpose |
|---|---|
| `[DIR]` | Directory (or file) of contract YAML. Default `.`. |
| `--against <benchmark.json>` | Benchmark artefact to evaluate against. Required. |
| `--format {markdown,json}` | Output format. `markdown` (default) is the PR-comment shape; `json` is for machine composition. |
| `--output PATH` | Also write the rendered report to `PATH`. |

Each contract is matched to the benchmark scenario of the same task
name. Budget clauses are checked against the scenario's stats; outcome
clauses are shown tagged **(advisory)** and never fail the build.

| Verdict | Condition | Exit code |
|---|---|---|
| passed | every budget clause comfortably within its ceiling | 0 |
| passed with warnings | a clause within 10% of its ceiling/floor | 1 |
| failed | at least one budget clause violated | 2 |

Clauses the benchmark can't measure (`max_tool_result_tokens`,
`max_run_duration_seconds`) are reported as "not checked" and enforced
at runtime instead.

To post a single combined PR comment covering both the cost diff and
the contract verdict, pass `--contracts` to `inkfoot diff`:

```bash
inkfoot diff baseline.json current.json --contracts ./contracts
```

The combined exit code is the more severe of the two reports.

## `inkfoot tail`

Stream events live from the database — one line per event, as
they're inserted. Useful for watching an agent's calls + smells
land while you iterate locally, or for piping to `grep` /
`jq` / `awk` in a tight debugging loop.

```bash
inkfoot tail --task customer-support-triage --since 10m
```

| Flag | Purpose |
|---|---|
| `--task <name>` | Only show events on runs whose `task` matches this value. |
| `--since <duration>` | Backfill events from this window before tailing live. Same `<n><unit>` shape as `--last` (`10m`, `2h`, `7d`). Default: no backfill. |
| `--poll-interval-ms <n>` | Storage poll cadence. Default `200` ms; tighten for snappier output, loosen on a busy DB. |
| `--max-iterations <n>` | Exit after `n` polls. Mostly useful for tests and one-shot scripts; omit for an unbounded tail. |
| `--db <path>` | Override the default database path. |

Output is a compact one-liner per event:

```
14:32:18.401  llm_call       6T29A8GH  provider=anthropic model=claude-haiku-4-5 input_tokens=410 output_tokens=27 cost_nd=4150000
14:32:18.502  outcome        6T29A8GH  outcome=success quality=0.94
```

The columns are:

| Column | Meaning |
|---|---|
| `HH:MM:SS.mmm` | Local time of the event's `occurred_at`. |
| `kind` | Event kind (`llm_call`, `outcome`, `smell`, `checkpoint`, …). |
| `run-short` | The last eight characters of the run id, with a leading `run-` prefix stripped first so the suffix never cuts across the hyphen. Padded for alignment. |
| `key=value …` | Per-event-kind projection of the most useful payload fields. Long values are truncated with `…` so the line doesn't wrap. |

Exit with `Ctrl-C`. The poll loop reads from the same SQLite
database the shim writes to, so the tail picks up events from any
process talking to the same `--db` path.

## CI cost review recipes

The simplest way to wire `inkfoot benchmark` + `inkfoot diff` into
a pull-request workflow is the published GitHub Action. The
end-to-end recipe — including the action's `baseline-source` DSL,
the threshold presets, and a sample workflow file —
lives in [Set up CI cost review](../recipes/set-up-ci.md), which
is the source of truth for the action's input shape.

Non-GitHub setups:

- [CI on GitLab](../recipes/ci-gitlab.md) — `.gitlab-ci.yml` with
  a sticky MR note via `gh` / `curl`.
- [CI on Bitbucket](../recipes/ci-bitbucket.md) — same flow for
  `bitbucket-pipelines.yml`.

## Tips

- `--version` is supported at the top level: `inkfoot --version`.
- Commands that read the SQLite event log default to
  `~/.inkfoot/runs.db`. Override with `--db <path>` or by setting
  `INKFOOT_HOME=<dir>` so the directory becomes the new parent.
  See the [top of this page](#cli-reference) for the exact list
  of subcommands that accept `--db`.
- The CLI is the front of the same SQLite database your instrumented
  process writes to. You can also query it directly with `sqlite3` for
  bespoke analyses.
