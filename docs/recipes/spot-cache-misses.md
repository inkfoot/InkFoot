# Recipe: Spot cache-miss patterns

Provider prompt caches (Anthropic `prompt_caching`, OpenAI
`prompt_caching`) only fire when the prefix is **byte-identical
call-over-call**. The classic killer is a single drifting token
inside the system prompt — a timestamp, a request id, a per-user
preamble — that pushes the prompt across the byte-identity line
and forces a full cache miss. This recipe walks through finding
that pattern and fixing it. Target: under ten minutes.

## What you'll need

- Inkfoot ≥ 0.1 with `inkfoot.instrument()` running in your app.
- A task with at least a few recorded runs whose model supports
  prompt caching (Anthropic Sonnet / Haiku, OpenAI GPT-4o, etc.).

## 1. Look for the smell

```bash
inkfoot report --last 7d --group-by task
```

Look at the `Aggregate smells` stanza at the bottom of the
output. The two cache-related smells are:

- `unstable-prompt-prefix` — your system block drifts
  call-over-call; the provider's cache never warms.
- `recurring-cache-writes` — the call writes to the cache but the
  cache hits aren't materialising. This usually means the *cache
  block* is being created in the wrong place.

If either fires on more than ~10% of runs for a task, drill in.

## 2. Confirm on a specific run

Grab any run that fired the smell:

```bash
inkfoot report --last 24h --task <name> | head
inkfoot report --run run-01JX...
```

In the attribution chart, watch the `system_dynamic` row. A
healthy cacheable system block has `system_static` ≫
`system_dynamic`; an unhealthy one has the two within 10× of each
other.

The smell stanza names the cause:

```
Smells detected (1):
  · unstable-prompt-prefix  (cache-breaker)
    → Move time-varying content (timestamps, request IDs,
      per-call context) out of the system block.

Estimated savings if fixed: ~$0.0042/run (-34%).
```

The "estimated savings" line is `system_dynamic_tokens × cache_read_rate` —
i.e. the cost you *would have* paid at the cheaper cache-read
rate if those tokens had stayed in the static prefix.

## 3. Find the drifting line

Drop into your code and look at how you build the system block.
Common culprits:

- `f"You are an agent. Today is {datetime.utcnow()}."`
- `f"User {user_id} wants help with..."` (the user identifier
  varies per call, so the prefix never stabilises across users).
- A request id, trace id, or A/B variant string interpolated
  into the preamble.

The fix shape is the same in every case: **move the drifting
content out of the system block** and into the first user message
(or a tool-result block). The system block becomes a constant
that the provider can cache; the per-call content rides the
non-cacheable suffix where it belongs anyway.

## 4. Optional: let Inkfoot do the cache-control placement

If you're on Anthropic and you want Inkfoot to manage cache-block
placement automatically, register the
[`CacheControlPlacer`](../concepts/observation-policies.md)
policy at startup:

```python
import inkfoot
from inkfoot.policy import CacheControlPlacer

inkfoot.instrument(policies=[CacheControlPlacer()])
```

The policy is observe-only today (it surfaces the placement
recommendation in the report and the smell evidence rather than
mutating the request). The mutating variant is on the roadmap;
the observe-only mode is enough to verify the prefix is stable
end-to-end.

## 5. Verify the fix

After you ship:

```bash
inkfoot report --last 24h --task <name>
```

If the smell disappears from the `Aggregate smells` stanza and
the `cache_read_tokens` field in the single-run view starts
appearing (it didn't before — the cache hadn't warmed), the fix
is in. Per-run cost should drop in proportion to how much of the
total input was the previously-drifting prefix.

## Next step

The single-run report shows you which runs already hit the cache.
For a longer baseline:

→ [Find your most expensive agent](find-expensive-agent.md) —
spot the next worst offender once the cache-miss money is
recovered.
