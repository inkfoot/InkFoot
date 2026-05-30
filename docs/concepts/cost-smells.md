# Cost Smells

A *cost smell* is a named pattern in a run's token attribution that is
almost always wasted money. Inkfoot ships five built-in smells; the
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
| `oversized-tool-result-recycled` | warn | `tool_result_tokens` | `CheapSummariser` (future) |
| `expensive-model-low-entropy` | info | `output_tokens` | — |
| `recurring-cache-writes` | warn | `cache_creation_tokens` | `CacheControlPlacer` |

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
across turns. Until automatic summarisation lands as a future policy,
prune the messages array manually after each tool invocation — keep a
short summary of the result, drop the full body.

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
