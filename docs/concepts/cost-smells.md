# Cost Smells

A *cost smell* is a named pattern in a run's token attribution that is
almost always wasted money. Inkfoot ships eleven built-in smells; the
engine evaluates them every time you render a report, and each detected
smell appears in the "Smells detected" section with a recommendation
and an estimated cost impact.

This page covers what each smell detects, why it costs you, what to do
about it, and what the suggested policy is.

## Smell summary

| ID | Severity | Anchored to | Suggested policy |
|---|---|---|---|
| `unstable-prompt-prefix` | warn | `system_dynamic_tokens` | `CacheControlPlacer` |
| `runaway-retry-loop` | critical | — (run shape) | `RetryThrottle` |
| `oversized-tool-result-recycled` | warn | `tool_result_tokens` | [`CheapSummariser`](modification-policies.md) |
| `expensive-model-low-entropy` | info | `output_tokens` | — |
| `recurring-cache-writes` | warn | `cache_creation_tokens` | `CacheControlPlacer` |
| `summariser-quality-regression` | critical | `summariser_tokens` | — |
| `tool-schema-drift` | warn | `tool_schema_tokens` | [`LazyToolExposure`](modification-policies.md) |
| `cost-skewed-by-outlier` | warn | — (cross-run) | [`BudgetCap`](observation-policies.md) |
| `unbounded-conversation-history` | warn | `memory_tokens` | — |
| `over-instrumented-retries` | warn | `retry_overhead_tokens` | [`RetryThrottle`](observation-policies.md) |
| `summariser-not-firing` | warn | `tool_result_tokens` | [`CheapSummariser`](modification-policies.md) |

Smells are evaluated lazily. They never touch the SDK hot path; they
only run when you ask for a report.

## `unstable-prompt-prefix`

**Trigger.** More than 10% of the run's system block is dynamic — i.e.
the system prompt drifts call-over-call.

**Why it costs you.** The provider's prompt cache only fires when the
prefix is byte-identical from one call to the next. When the prefix
drifts, every otherwise-cacheable token is billed at the full input rate
instead of the (much cheaper) cache-read rate. The usual culprit is a
timestamp, request ID, or per-call user-context line embedded *inside*
the system block.

**Evidence in the report.** Lists `system_static_tokens`,
`system_dynamic_tokens`, the computed `dynamic_fraction`, and the
threshold.

**Estimated cost impact.** `system_dynamic_tokens × cache_read_price` —
a lower bound on what you could have paid if the prefix were stable.

**Recommendation.** Move time-varying content (timestamps, request IDs,
per-call context) out of the system block and into a normal user
message. Or split the system block so the static portion is on its own
content block and stays cacheable.

## `runaway-retry-loop`

**Trigger.** The same tool is invoked more than 5 times in a single
run.

**Why it costs you.** The classic "agent stuck in a loop" pattern. A
tool whose result the agent can't interpret will be retried with
marginally different inputs. Each retry pays for the full prompt and
tool-result history again — cost grows roughly quadratically in the
worst case.

**Evidence in the report.** The tool name that breached, the call count,
the full distribution of tool calls in the run, and the total accumulated
retry-overhead tokens.

**Estimated cost impact.** Sum of `retry_overhead_tokens` across all
events × the input rate. (Until retry classification is enabled, this
field may be zero — the smell still fires so you see the loop, just
without a dollar figure.)

**Recommendation.** Inspect the tool's output for ambiguity or a
missing exit condition in the agent's loop. Add a guard that breaks
after N attempts or escalates to a human. Enable the `RetryThrottle`
policy so future runs that re-enter the loop emit a `retry_throttle`
event you can alert on.

## `oversized-tool-result-recycled`

**Trigger.** A tool result of more than 2000 tokens sits in the
messages array across 3 or more turns.

**Why it costs you.** Once a tool result is in the messages array, it
ships back to the model on *every subsequent turn* — and you pay full
input rate for it every time. A 5000-token search result that hangs
around for ten turns has cost you 50,000 input tokens, only one
turn's worth of which was actually useful.

**Evidence in the report.** The tool name, the result size in tokens,
and the number of turns it has been recycled.

**Estimated cost impact.** `tool_result_tokens × (turns - 1) ×
input_price`.

**Recommendation.** Summarise large tool results before recycling them
across turns. If you run under a framework adapter, register the
[`CheapSummariser`](modification-policies.md) policy and the
replacement happens automatically; otherwise prune the messages array
manually after each tool invocation — keep a short summary of the
result, drop the full body.

## `expensive-model-low-entropy`

**Trigger.** A call uses an expensive model (Opus, gpt-4o, o-series)
with `reasoning_tokens == 0` and `output_tokens < 200`.

**Why it costs you.** Short, non-reasoning responses are easy work. A
cheaper model (Haiku, gpt-4o-mini) would have produced the same answer
for a small fraction of the cost.

**Severity is `info`, not `warn`.** Sometimes the expensive model is
the right call — latency profile, instruction-following, or your own
benchmark says so. The smell is purely informational so you don't have
to act on it, but the cost difference is usually worth a look.

**Evidence in the report.** The model name, `output_tokens`, and
`reasoning_tokens`.

**Estimated cost impact.** `output_tokens × (model_price -
cheap_alternative_price)`.

**Recommendation.** Route short, non-reasoning prompts (classification,
key extraction, formatting) to a cheaper model. A named-routing
wrapper is the typical fix until automatic model routing ships.

## `recurring-cache-writes`

**Trigger.** More than 80% of the run's calls wrote to the prompt
cache (`cache_creation_tokens > 0`).

**Why it costs you.** Cache writes cost roughly 25% *more* than fresh
input on Anthropic. They only pay off if subsequent calls actually read
from the cache — break-even is around four reads per write. A run that
keeps writing without amortising is *thrashing the cache*: every call
invalidates the prior write because the section after the
`cache_control` marker drifted.

**Evidence in the report.** Per-call `cache_creation_tokens` counts and
the percentage of calls that wrote to cache.

**Estimated cost impact.** `sum(cache_creation_tokens) ×
cache_write_premium`.

**Recommendation.** Move the `cache_control` marker earlier in the
prompt — before the unstable section — so the static prefix gets cached
and the per-call drift becomes a normal input read instead of a fresh
write.

## `summariser-quality-regression`

**Trigger.** The [`CheapSummariser`](modification-policies.md) policy,
running in trust mode, found that runs receiving summarised tool
results succeed measurably less often than runs that kept the raw
results, and auto-disabled itself for the task. The smell surfaces
that finding on the run where the comparison tipped.

**Why it matters.** This is the one built-in smell about *quality*
rather than wasted tokens. Summarisation only pays off if the agent
still completes its task; when the A/B comparison shows the summarised
branch trailing, the token savings are being bought with failed runs —
usually a far worse trade.

**Evidence in the report.** The task name, run counts and success
rates for both branches, the measured success-rate drop, the
quality-score delta (when quality scores were recorded), and the
configured threshold.

**Estimated cost impact.** None — the cost of this smell is degraded
task quality, not tokens, so no dollar figure is estimated.

**Recommendation.** Inspect the summarised tool results preserved in
the event log to see what the summaries dropped. Raise the
summariser's `threshold_tokens` or `max_summary_tokens` so more
context survives, or leave it disabled for the task. Re-enable with
`enable_summariser_for_task()` once the configuration changes.

## `tool-schema-drift`

**Trigger.** The run's tool-schema fingerprint changes between calls —
tools were added, removed, or reordered partway through the run.

**Why it costs you.** Tool schemas serialise near the top of the
request body, so a mid-run change breaks the provider's prompt cache
for every call from that point on: the schema block (and everything
after it) re-tokenises at full input rate instead of being served from
cache. The detector keys on the stable fingerprint adapters stamp per
call rather than on the per-call tool list, so policies like
[`LazyToolExposure`](modification-policies.md) that legitimately narrow
exposure never trip it. Runs whose calls carry no fingerprint (raw SDK
shims without an adapter) stay silent.

**Evidence in the report.** The distinct fingerprints in first-seen
order, the sequence of the first change, and the number of calls and
`tool_schema_tokens` from the change onward.

**Estimated cost impact.** `tool_schema_tokens` after the first change
× cache-read price — the optimistic floor: even served perfectly from
cache, those schema tokens would still cost this much.

**Recommendation.** Register every tool up front and keep the ordering
deterministic for the lifetime of a run. If the agent only needs a
subset of tools per call, narrow exposure with
[`LazyToolExposure`](modification-policies.md) instead of mutating the
registered set.

## `cost-skewed-by-outlier`

**Trigger.** The run cost more than 10× the median of its task's
recent runs. Needs at least 5 peer runs — a task's second-ever run is
often 10× its first, and that's not an outlier, that's noise.

**Why it costs you.** One run like this drags the task's average far
above what a typical run costs, so per-task aggregates stop reflecting
reality. The usual causes are a retry storm, a runaway loop, or an
unusually large input that deserves its own task name.

**Cross-run, report-only.** This is the one built-in smell that needs
context beyond the run itself. `inkfoot report` supplies the peer
median when it evaluates smells; in-process evaluation without that
context stays silent rather than guessing.

**Evidence in the report.** The run's cost, the task's peer median,
the peer count, and the computed ratio.

**Estimated cost impact.** `run_cost − peer_median` — the excess over
a typical run, in dollars directly.

**Recommendation.** Investigate what made the run exceptional, and
consider a [`BudgetCap`](observation-policies.md) so a single run
cannot overshoot the task's typical cost unbounded.

## `unbounded-conversation-history`

**Trigger.** Any single call in the run carries more than 50,000
tokens of conversation history / agent memory. The threshold applies
to the largest single call, never the sum — history is recycled
context, so summing across turns would count the same tokens once per
turn.

**Why it costs you.** Nothing is trimming the history, so every
additional turn re-sends the whole transcript and the per-call cost
grows linearly until the model's context window runs out.

**Evidence in the report.** The largest call's `memory_tokens`, the
number of breaching calls, and the total excess above the threshold.

**Estimated cost impact.** The per-call excess above the threshold,
summed over breaching calls, × cache-read price — history is a stable
prefix, so even served from cache the excess still bills at the
cache-read rate; trimming recovers at least this much.

**Recommendation.** Add memory compression — fold older turns into a
running summary — or truncate history beyond a fixed turn window.

## `over-instrumented-retries`

**Trigger.** Failed calls outnumber completed calls by more than 3 to
1. The event stream has no call-site identity, so "retries per call"
is approximated as failed ÷ completed `llm_call` events; a run with no
completed calls at all still fires once it racks up enough failures.

**Why it costs you.** The upstream is failing (rate limits, timeouts,
overload) and the agent keeps re-sending the same request — each
attempt re-tokenises the full context, so a persistent failure
multiplies the run's cost without producing anything.

**Evidence in the report.** Failed and completed call counts, the
ratio, a tally of error types, and any `retry_throttle` events already
present (proof the policy is pushing back).

**Estimated cost impact.** Total `retry_overhead_tokens` × input
price. Until retry classification is enabled this field is usually
zero — the smell still fires on the run shape alone, just without a
dollar figure.

**Recommendation.** Tune the SDK's backoff (fewer attempts, longer
waits) and circuit-break the upstream after repeated failures.
[`RetryThrottle`](observation-policies.md) enforces a retry budget per
window at the instrumentation layer.

## `summariser-not-firing`

**Trigger.** Three or more calls each carry over 2000 tokens of raw
tool results, and no summariser activity appears anywhere in the run —
no summariser-stamped call, no `summariser_tokens` in any ledger, no
summariser policy events.

**Why it costs you.** Every oversized result is billed at full input
rate on every call it stays in context. A summariser folds those
bodies into a few hundred tokens before they re-enter the
conversation. A configured summariser whose threshold is set so high
it never fires is indistinguishable from no summariser — which is
exactly the situation worth flagging.

**Sibling smell.** `oversized-tool-result-recycled` catches one big
result being recycled across turns; this smell catches the broader
pattern of consistently oversized results with no summarisation in
place.

**Evidence in the report.** The oversized call count, the largest
result's size, and the total excess above the threshold.

**Estimated cost impact.** The per-call excess above 2000 tokens,
summed over oversized calls, × input price — the tokens a summariser
would have removed, at the rate they cost today.

**Recommendation.** Enable
[`CheapSummariser(threshold_tokens=1500)`](modification-policies.md)
so oversized tool results are folded into short summaries before they
recycle through the context.

## Reading the impact summary

When one or more smells fire, the report's footer estimates total
recoverable cost:

```
Estimated savings if fixed: ~$0.0085/run (-12%).
```

Treat this as a rough floor, not a precise prediction. The numbers are
deliberately conservative — they assume the perfect cache or the
perfect retry-guard fix, which real-world changes rarely achieve in
one pass.

## Inspecting evidence directly

Each `CostSmell` carries an `evidence_query` string — a SQL or JSONPath
expression that reproduces the underlying evidence for that smell. Use
it to audit a finding against the raw events table without re-running
the engine:

```python
from inkfoot.smells import get_smell
print(get_smell("unstable-prompt-prefix").evidence_query)
```

## Extending with your own smells

The smell registry is open. To add a custom smell, build a `CostSmell`
and register it:

```python
from inkfoot.smells import CostSmell, DetectionResult, register_smell

def _detect_my_pattern(run, events):
    ...
    return DetectionResult(
        smell=MY_SMELL,
        triggered_at_sequence=...,
        severity="warn",
        evidence={...},
        estimated_cost_impact_nd=...,
    )

MY_SMELL = CostSmell(
    id="my-org/spurious-system-edit",
    title="...",
    description="...",
    severity="warn",
    detect=_detect_my_pattern,
    recommendation="...",
)

register_smell(MY_SMELL)
```

After registration, your smell evaluates alongside the built-ins on
every report.

To share a smell with everyone rather than keeping it local, contribute
it to the open [Cost Smell Library](cost-smell-library.md) — the same
catalogue these built-in smells are published through, bundled with the
package and browsable on the web.
