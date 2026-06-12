# Recipe: Find your most expensive agent

You've got several tasks instrumented and a week of runs in the
database. Which one is burning the budget? This recipe walks
through the aggregate view, the per-task breakdown, and a drill-
in to the worst offender. Target: under ten minutes.

## What you'll need

- Inkfoot installed, with at least a few days of recorded runs
  (`pip install inkfoot` + `inkfoot.instrument()` in your app).
- The CLI on your `$PATH`: `which inkfoot` should return a path.

## 1. Find the worst task

```bash
inkfoot report --last 7d --group-by task
```

The output is a sorted table — biggest bucket spend first:

```
Recent runs (7d, grouped by task):

  bucket                   runs  cost/success  cost/accepted_answer    avg_$    p95_$  success%
  customer-support-triage   142       $0.0125                     —  $0.0118  $0.0341     94.3%
  invoice-extraction         37       $0.0484               $0.0533  $0.0432  $0.0982     89.2%
  meeting-summariser         21       $0.0070                     —  $0.0067  $0.0145     95.2%
  uninstrumented             14             —                     —  $0.0102  $0.0239         —

Aggregate smells (last 7d):
  · unstable-prompt-prefix: 18/200 runs (9%)
  · oversized-tool-result-recycled: 7/200 runs (4%)
```

Read the table left-to-right:

- **`runs`** — call volume in the window.
- **`cost/success`** — the headline: bucket spend ÷ successful
  runs, so the money burned on failures is priced in. Usually the
  right north-star for "what does it cost me to actually solve a
  ticket?" — here `invoice-extraction` is the worst offender even
  though `customer-support-triage` spends more in total.
- **`cost/accepted_answer`** — the same ratio for the
  human-accepted outcome tier (review workflows). `—` when no run
  in the bucket reported it.
- **`avg_$` / `p95_$`** — typical and tail per-run cost. A
  big gap between the two flags a long-tail problem (most runs
  cheap, some explosively expensive).
- **`success%`** — outcome rate. Cheap runs that fail aren't a
  bargain.

The `uninstrumented` row pinned at the bottom collects runs that
never reported an outcome — totalled separately and excluded from
every rate, so partial coverage can't skew the instrumented
buckets. See [Cost per Success](../concepts/cost-per-success.md)
for the reasoning behind these columns.

The bottom `Aggregate smells` stanza tells you which named
patterns are firing across the window, and on what fraction of
runs. That's the "what's likely wrong" preview before you drill
in.

## 2. Drill into the worst offender

Pick the task with the worst `cost/success` and grab a recent run
of it from the `inkfoot report --last 24h --task invoice-extraction`
output. Then render the single-run view:

```bash
inkfoot report --run run-01JX7...
```

The attribution chart shows where the money went on that
specific run:

```
Run run-01JX7... · invoice-extraction · 8.4s · $0.0432 · success (0.91)

Causal attribution:
  tool_result         58.6%  ███████░░░░░  $0.0253  ⚠ oversized
  system_static       17.4%  ██░░░░░░░░░░  $0.0075
  output               9.3%  █░░░░░░░░░░░  $0.0040
  ...

Smells detected (1):
  · oversized-tool-result-recycled  (oversized)
    → Summarise large tool results before recycling them across turns.

Estimated savings if fixed: ~$0.0190/run (-44%).
```

The bar chart names the largest field; the smell stanza tells
you *why* and *what to do*. In this case more than half the cost
is `tool_result_tokens` — the agent's tool calls are returning
big payloads and the agent loop is re-including the full results
in every subsequent turn.

## 3. Decide what to change

Two patterns cover most "expensive agent" cases:

- **Cost in a single category that the smell engine names** —
  follow the smell's recommendation. `oversized-tool-result-recycled`
  wants you to summarise tool results before re-including them;
  `unstable-prompt-prefix` wants you to move time-varying content
  out of the system block; `expensive-model-low-entropy` wants
  you to demote to a cheaper model for the affected calls.
- **Cost spread evenly across categories** — the call is just
  *big*. Consider whether the task can split into multiple
  smaller agent calls, or whether the cheapest reasonable model
  for the task is actually being used.

For both, the [Cost Smells](../concepts/cost-smells.md) catalogue
is the index; each smell entry names the policy (when one exists)
that mechanises the fix.

## 4. Verify the fix

After you ship the change, watch the aggregate view tighten over
the next 24 hours:

```bash
inkfoot report --last 24h --group-by task --task invoice-extraction
```

If the smell stops appearing in the `Aggregate smells` stanza and
`cost/success` drops, you're done. If `cost/success` drops but a
new smell shows up, the fix may have shifted cost from one
category to another — chase the new smell next.

## Next step

Once you have the worst-offender pattern in hand, wire the CI
cost-review workflow so the next prompt-induced regression
surfaces on the pull request that introduces it:

→ [Set up CI cost review](set-up-ci.md)
