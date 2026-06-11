# Performance & Overhead

Inkfoot is meant to live in your agent's hot path, so it has explicit
performance budgets for every operation it puts between your code and
the LLM provider. This page documents what those budgets are, what they
cover, and how you can verify them yourself.

## At a glance

| Operation | Budget (p95) | Budget (median) | What it covers |
|---|---|---|---|
| SDK shim wrapper (metadata mode) | 1 ms | 300 µs | The work the shim does on every `messages.create` / `chat.completions.create` — translator + emit + storage write. |
| SDK shim wrapper (replay mode) | 5 ms | — | Same as above, plus JSON-serialising the request and response into the `event_contents` table. |
| Storage event insert | 1 ms | 500 µs | One `INSERT` into `events` plus the dirty-flag update, fsynced via WAL. |
| Aggregator drain (50 runs) | 50 ms | 50 ms | The background thread that recomputes `runs.total_*` from the event log. |
| Smell engine (per run) | 20 ms | 10 ms | All built-in smells evaluated against a 50-event run. |
| Report renderer | 200 ms | 100 ms | `inkfoot report --run <id>` for a 50-event run, end-to-end. |

These numbers describe the **work Inkfoot does** — not the LLM call
itself. The LLM round-trip is typically 100–10000× longer than the
shim overhead, so the instrumentation never shows up in your agent's
end-to-end latency.

## Where the time goes

When you call `client.messages.create(...)` on an instrumented SDK,
the call returns the original SDK response, untouched. Between
entering the shim and that return:

1. **Before-call hooks** run for every registered policy
   (`BudgetCap`, `RetryThrottle`, `CacheControlPlacer`, plus any
   custom policies). Each hook is wrapped in exception isolation so a
   broken policy can never break your agent. One documented
   exception to the hook budgets: when the
   [`CheapSummariser`](modification-policies.md) modification policy
   decides to replace an oversized tool result, its hook makes a
   cheap-model LLM call — a real network round-trip, made at most
   once per unique result and excluded from the budgets below.
2. **The original SDK call executes** — this is the LLM round-trip
   itself, which dominates wall-clock time.
3. **The translator** converts the response into a `NeutralCall` with
   the fourteen-field ledger populated. Tokeniser dispatch happens
   here.
4. **After-call hooks** run for every registered policy.
5. **One event row is inserted** into `events`, plus the `runs` row's
   `aggregates_dirty` flag is flipped. Both happen in a single
   transaction.
6. In replay mode, **one `event_contents` row is inserted** in the
   same transaction with the serialised request and response.

The shim returns the original response from step 2 unchanged.

The budgets above cover steps 1, 3, 4, 5, and (in replay mode) 6 —
i.e. all the work Inkfoot adds on top of the SDK call.

## Replay mode trade-off

`inkfoot.instrument(capture_mode="replay")` writes the full request
and response bodies into the `event_contents` table. The serialisation
itself dominates the additional cost — typically a few hundred
microseconds for a small request, into the low milliseconds for a
large multi-turn message array.

If your agents send big requests (lots of tool definitions, long
message histories, large retrieved context), expect replay mode to
land at the upper end of its 5 ms p95 budget. The default
`capture_mode="metadata"` adds no serialisation and stays well under
the 1 ms p95 budget.

## What about long tails?

The medians above hold steady on a modern developer laptop. P95
budgets are deliberately set higher than the median so that occasional
slow-path events (page faults, GC pauses, disk syncs) don't generate
false alarms. If you observe p99 latency that's a small multiple of
the p95 budget, that's normal for a busy host.

Persistent regressions show up as a regression in the *median*, not
just an outlier in the tail — that's the signal worth investigating.

## Asynchronous work

The aggregator runs on a background thread and never blocks your
agent. Its 50 ms drain budget is for catching up on 50 dirty runs in
one pass; it sleeps between passes (500 ms by default — set
`INKFOOT_AGGREGATOR_INTERVAL_MS` to override).

This means the `total_nanodollars` and `outcome` columns on `runs`
can be up to one poll interval behind reality. Reports that need
strict consistency should run `inkfoot rebuild-aggregates` first.

## Smell engine and report renderer

Smells are evaluated **lazily**. They never touch the SDK hot path —
they only run when you call `inkfoot report` (or build a renderer of
your own that uses `SmellEngine`).

The report renderer's 200 ms p95 budget covers everything from
opening the database to printing the final newline, on a 50-event run
with all built-in smells active. Long-tail runs (hundreds of events) are
proportionally larger but still complete in well under a second.

## Verifying the budgets yourself

The Inkfoot project ships its full benchmark suite with the source.
If you've cloned the repo from source, you can run them locally:

```bash
pytest tests/benchmarks --benchmark-only
```

To produce a JSON artefact of the run for trend tracking:

```bash
pytest tests/benchmarks --benchmark-only \
    --benchmark-json=benchmark.json
```

Each benchmark asserts its own budget — a failure means a regression
against the published numbers above. The benchmarks run against an
in-process fake SDK and a temp-file SQLite database so the result
reflects Inkfoot's own overhead with no network or shared-runner
noise.

## How budgets are enforced

The benchmarks above run on every pull request to Inkfoot. The build
fails if a budget is breached and the benchmark JSON is uploaded as a
CI artefact so trends are reviewable across PRs.

This means the numbers in the table at the top of this page aren't
aspirational — they're the actual gates the codebase rides under.
