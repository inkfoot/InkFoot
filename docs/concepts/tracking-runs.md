# Tracking Runs

A *run* is one unit of agent work. Inkfoot needs you to mark its
boundaries so it can attribute LLM calls to a meaningful piece of work
and so reports can group calls together.

This page covers:

- `inkfoot.agent_run(...)` — decorator, context manager, async, manual.
- `inkfoot.set_outcome(...)` — record whether the run succeeded.
- `inkfoot.tag(...)` — attach structured metadata to a run.
- `inkfoot.tag_retrieval(...)` — record retrieved-context tokens.
- `inkfoot.report_cost()` — read the run's running total.

All five raise `inkfoot.errors.InkfootError` (specifically `NoActiveRun`)
if called outside an active run.

## `inkfoot.agent_run(task=..., metadata=...)`

The recommended way to bracket a run.

### As a decorator

```python
@inkfoot.agent_run(task="customer-support-triage")
def handle_ticket(ticket_id: str):
    ...
```

Each call to `handle_ticket` is one run. The decorator is async-aware:
decorate an `async def` and it wraps the coroutine correctly.

### As a context manager

```python
with inkfoot.agent_run(task="invoice-extraction") as run:
    extract(invoice_id)
    inkfoot.set_outcome("success")
print(f"finished run {run.id}")
```

Async variant uses `async with`:

```python
async with inkfoot.agent_run(task="..."):
    await do_work()
```

### Manual start / end

For cases where the run lifetime doesn't match a function call:

```python
run = inkfoot.agent_run(task="long-investigation").start()
try:
    do_work()
    run.end(status="complete")
except Exception as exc:
    run.end(status="error", error_message=str(exc))
    raise
```

### What it does

When the run starts:

- A new row is inserted into `runs` with `status='running'`, the supplied
  `task`, and a `parent_run_id` pointing at the surrounding run (if any —
  runs nest cleanly).
- A `run_start` event is appended.
- A `contextvars`-backed pointer to the new run becomes the *current run*
  so any LLM calls happening inside this scope are attributed correctly.

When the run ends:

- A `run_end` event is appended with `status='complete'` or
  `status='error'` (plus the truncated exception text on error).
- The `runs` row is updated to `complete` / `error` with `ended_at` set.
- The current-run pointer is restored to its previous value.

### Abandonment

If your process exits between `start_run` and `end_run` (for example, a
hard kill), the run is left at `status='running'`. The next time Inkfoot
starts up, its shutdown hook flips any leftover `running` rows to
`status='error'` with `error_message='abandoned'`. You will never see a
zombie row in reports.

## `inkfoot.set_outcome(outcome, quality_score=None)`

Record the user-visible result of the run.

```python
inkfoot.set_outcome("success", quality_score=0.94)
inkfoot.set_outcome("failure")
inkfoot.set_outcome("human_escalated")
```

| Argument | Allowed values |
|---|---|
| `outcome` | `"success"`, `"failure"`, or `"human_escalated"`. |
| `quality_score` | A float in `[0.0, 1.0]`, or `None`. Out-of-range values raise. |

The outcome shows up in reports (`success (0.94)`), in the aggregate view's
`success%` column, and is recorded on the `runs.outcome` and
`runs.quality_score` columns by the background aggregator.

`set_outcome` may be called more than once in a run; the last value wins
during aggregation.

## `inkfoot.tag(key, value)`

Attach an arbitrary JSON-scalar tag to the active run.

```python
inkfoot.tag("user_tier", "enterprise")
inkfoot.tag("retries", 3)
inkfoot.tag("ab_variant", True)
```

Allowed value types: `str`, `int`, `float`, `bool`, `None`. Complex
objects (dicts, lists, custom classes) are rejected at the boundary so
tags stay grep-friendly in reports.

Tags emit a `user_tag` event in the run's event stream. You can attach
many tags per run; reports show them as a flat list.

## `inkfoot.tag_retrieval(text)`

Tells Inkfoot that the next LLM call will include `text` as retrieved
context (RAG results, knowledge-base lookups, etc.). The text is
tokenised and added to the **next** call's `retrieved_context_tokens`
ledger field.

```python
docs = my_retriever.search(query)
inkfoot.tag_retrieval("\n".join(d.content for d in docs))
response = client.messages.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": prompt_with_docs}],
)
```

You can call `tag_retrieval` multiple times before a single LLM call
(e.g. several chunks from a retriever); Inkfoot sums them and lifts the
total into the next call's ledger.

## `inkfoot.report_cost()`

Returns the current run's accumulated cost as a `decimal.Decimal` in
USD. Useful for printing live budget information or for in-process
guards.

```python
with inkfoot.agent_run(task="batch-summarise"):
    for doc in docs:
        process(doc)
        if inkfoot.report_cost() > Decimal("0.50"):
            inkfoot.set_outcome("failure")
            raise CostExceededError("$0.50 budget exceeded")
```

The value comes from the background-aggregated `total_nanodollars`
column, so it may lag the most recent call by up to one aggregator poll
interval (500 ms by default). For strict consistency, run `inkfoot
rebuild-aggregates` first.

## Putting it together

```python
import inkfoot
import anthropic

inkfoot.instrument()

@inkfoot.agent_run(task="customer-support-triage")
def handle_ticket(ticket_id: str, user_tier: str):
    inkfoot.tag("user_tier", user_tier)
    inkfoot.tag("ticket_id", ticket_id)

    kb_results = knowledge_base.search(ticket_id)
    inkfoot.tag_retrieval(format_kb_results(kb_results))

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=build_messages(ticket_id, kb_results),
    )

    quality = score_response(response)
    inkfoot.set_outcome(
        "success" if quality >= 0.7 else "human_escalated",
        quality_score=quality,
    )
    return response
```
