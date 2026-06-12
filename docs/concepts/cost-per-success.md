# Cost per Success

The cheapest agent in your fleet is not the one with the lowest
per-run average. It's the one that costs the least *per task actually
completed*. Cost-per-run hides failures: a task that averages $0.02
per run but succeeds half the time really costs $0.04 per result —
plus every retry, escalation, and abandoned attempt along the way.

That's why the aggregate report's headline column is `cost/success`:
bucket spend divided by successful runs, so the money burned on
failures is folded into the number instead of hidden behind an
average.

```console
$ inkfoot report --last 7d

Recent runs (7d, grouped by task):

  bucket             runs  cost/success  cost/accepted_answer   avg_$   p95_$  success%
  support-triage      412       $0.0190               $0.0440  $0.0110  $0.0260   58.0%
  doc-summarise       208       $0.0078                     —  $0.0071  $0.0102   91.3%
  uninstrumented       57             —                     —  $0.0093  $0.0210        —
```

Read that table top to bottom:

- `support-triage` *looks* cheaper per run than its `cost/success`
  suggests — $0.011 average, but a 58% success rate means each
  successful triage really costs $0.019.
- `doc-summarise` has a higher nominal success rate and a
  cost/success barely above its average — failures are rare enough
  not to distort the economics.
- The `uninstrumented` row is the coverage gauge (more below).

## The columns

| Column | Meaning |
|---|---|
| `runs` | Runs in the bucket within the window. |
| `cost/success` | Bucket spend ÷ runs with outcome `success`. `—` when the bucket has no successes. |
| `cost/accepted_answer` | Bucket spend ÷ runs with outcome `accepted_answer`. `—` when none. |
| `avg_$` | Bucket spend ÷ all runs — the classic per-run average, kept for distribution shape. |
| `p95_$` | 95th-percentile run cost in the bucket. |
| `success%` | Share of the bucket's runs with outcome `success`. |

`cost/accepted_answer` exists for review workflows. When a human
reviews agent output, plain `success` ("the agent finished") and
`accepted_answer` ("a person accepted the result") are different
economic tiers — an agent can finish cheaply and still produce
answers nobody accepts. Report both outcomes and the table prices
them separately.

## The `uninstrumented` row

Cost-per-success only works when runs report outcomes. Runs that
never call [`set_outcome`](tracking-runs.md) can't be averaged into
their task's bucket — their spend would count while their successes
couldn't, silently inflating `cost/success` for everyone else.

So the rollup diverts them into a dedicated `uninstrumented` row,
pinned to the bottom of the table: visible, separately totalled, and
excluded from every outcome rate. The row doubles as a coverage
gauge — a fat `uninstrumented` bucket means the fleet isn't
reporting outcomes yet, and the headline column is only as good as
its coverage.

## Reporting outcomes

The explicit API is one call at the end of the run:

```python
import inkfoot

with inkfoot.agent_run(task="support-triage"):
    answer = run_agent(ticket)
    inkfoot.set_outcome("success" if answer else "failure")
```

Four outcomes are recognised: `success`, `accepted_answer`,
`failure`, and `human_escalated`. See
[Tracking Runs](tracking-runs.md) for the full API.

## Inferring outcomes from framework results

For agents where "did it work?" is mechanically visible in the
framework's return value, `set_outcome_from_heuristic` saves writing
the same mapping boilerplate in every entry point:

```python
import inkfoot
from inkfoot.outcomes import set_outcome_from_heuristic

with inkfoot.agent_run(task="support-triage"):
    try:
        state = graph.invoke(inputs)   # LangGraph
    except Exception as exc:
        set_outcome_from_heuristic(error=exc)
        raise
    set_outcome_from_heuristic(state)
```

What it infers:

| You pass | Recorded outcome |
|---|---|
| A dict (LangGraph's final state — the graph reached END) | `success` |
| A result object with a populated payload (`final_output` / `output` / `data` / `raw` — OpenAI Agents SDK, Pydantic AI, CrewAI) | `success` |
| An exception — positionally or via `error=` | `failure` |
| `None`, a falsy value, or a result object with an empty payload | *nothing* |

The helper is deliberately conservative. When it can't tell, it makes
no `set_outcome` call and returns `None`, so the run lands in the
visible `uninstrumented` bucket instead of being guessed into the
wrong one. And it never auto-assigns `accepted_answer` or
`human_escalated` — no heuristic can know a human reviewed the
answer; those always need an explicit `set_outcome`.

The helper is duck-typed with no framework imports, so it costs
nothing when a framework isn't installed.

## Slicing by tag

`--group-by tag.<key>` buckets the same table by a
[user tag](tracking-runs.md) value instead of by task — cost per
success per customer tier, per A/B variant, per anything you tag:

```console
$ inkfoot report --last 7d --group-by tag.customer_tier

Recent runs (7d, grouped by tag.customer_tier):

  bucket       runs  cost/success  cost/accepted_answer   avg_$   p95_$  success%
  enterprise    118       $0.0312               $0.0590  $0.0220  $0.0480   70.3%
  free          494       $0.0058                     —  $0.0051  $0.0090   88.1%
  unknown        45       $0.0071                     —  $0.0066  $0.0102   93.3%
```

Runs that carry the tag bucket by its value (last write wins when a
run was tagged twice); runs that don't land in `unknown`, so a
partially-tagged fleet stays visible instead of dropping rows. See
the [CLI reference](../reference/cli.md) for the full `--group-by`
matrix.
