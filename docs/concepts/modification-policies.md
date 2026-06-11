# Modification Policies

Where [observation policies](observation-policies.md) only watch and
warn, *modification policies* rewrite the outgoing request before the
provider sees it — dropping stale tools from the `tools` array, or
replacing an oversized tool result with a cheap-model summary. The
original SDK response always comes back untouched; only the request is
edited.

Inkfoot ships two modification policies:

- `LazyToolExposure` — stop re-sending tool schemas the agent hasn't
  used or mentioned in a while.
- `CheapSummariser` — replace oversized tool results with a summary
  produced by a cheaper model.

Because they change what the model receives, modification policies are
only available where a **framework adapter** is active (LangGraph,
OpenAI Agents SDK, Anthropic Agent SDK, Pydantic AI). The adapter
gives Inkfoot enough context — turn boundaries, tool registries, run
identity — to edit requests safely. Registering a modification policy
on the plain SDK shim or the raw decorator raises
`inkfoot.errors.PolicyNotSupported` with a remediation hint.

## Registering modification policies

Activate a framework adapter first, then register:

```python
import inkfoot
from inkfoot.policy import (
    CheapSummariser,
    LazyToolExposure,
    register_policies,
)

inkfoot.instrument()                  # base instrumentation
inkfoot.langgraph.instrument(graph)   # activate the adapter

register_policies([
    LazyToolExposure(stale_after_turns=3),
    CheapSummariser(threshold_tokens=1500),
])
```

`register_policies()` consults the active adapter's
`supported_policies()` set. The LangGraph, OpenAI Agents, Anthropic
Agent, and Pydantic AI adapters support both modification policies.
The [CrewAI adapter](../frameworks/crewai.md) is the exception: CrewAI
doesn't expose the stable per-turn context (tool registry, turn
boundaries) that safe request rewriting needs, so it is
observation-only and rejects both. Observation policies pass
regardless of adapter. Passing a modification policy to
`inkfoot.instrument(policies=[...])` raises `PolicyNotSupported`
*before* any instrumentation is installed, so a misconfigured startup
fails fast rather than half-wiring.

## `LazyToolExposure(stale_after_turns=3, core_tools=())`

Every tool schema in the `tools` array is re-sent — and re-billed — on
every call. Agents routinely carry ten or more tool definitions of
which a given stretch of the conversation uses two. This policy
narrows the `tools` array per call to the tools that are plausibly
still relevant.

```python
LazyToolExposure(stale_after_turns=3, core_tools=("submit_answer",))
```

**Arguments.**

- `stale_after_turns` — the relevance window, a positive integer. A
  tool last called or mentioned at turn *T* stays exposed through turn
  `T + stale_after_turns` and is dropped after that.
- `core_tools` — tool names that are never dropped. A tool dict
  carrying a truthy `"inkfoot_core"` key is also exempt.

**Behaviour.**

- A tool counts as *relevant* when it was offered for the first time
  (every tool gets a fresh window on first sight), when the model
  invoked it (`tool_use` blocks on Anthropic, `tool_calls` on OpenAI),
  or when its name appears as a whole word in user or assistant
  message text. `"calc"` inside `"recalculate"` is not a mention;
  tool-result blocks are not scanned.
- Dropping a tool never removes knowledge — if a later user or
  assistant message references a dropped tool by name, it is restored
  on the next call.
- The policy never narrows the array to empty, and it never mutates
  the list object your framework supplied — it swaps in a fresh list
  on the outgoing request only.
- State is tracked per run; two concurrent runs never share turn
  counters.

**Event payloads.** Each transition lands in storage:
`lazy_tool_dropped` with `{"dropped": [...], "turn": N}` and
`lazy_tool_restored` with `{"restored": [...], "turn": N}`.

## `CheapSummariser(threshold_tokens=1500, ...)`

The [`oversized-tool-result-recycled`](cost-smells.md) smell tells you
*after the fact* that a large tool result was re-billed turn after
turn. This policy fixes it live: when an outgoing request carries a
tool result above the threshold, the policy asks a cheap model to
summarise it and sends the summary instead.

```python
CheapSummariser(
    threshold_tokens=1500,      # results at or below pass unchanged
    max_summary_tokens=600,     # hard ceiling on the replacement
    preserve_for_replay=True,   # keep the raw result in the event
)
```

**Arguments.**

- `threshold_tokens` — tool results at or below this size pass
  through unchanged.
- `max_summary_tokens` — hard ceiling on the replacement text. Model
  output that overruns is truncated to fit.
- `preserve_for_replay` — when `True` (the default), the raw result
  is kept in the `summariser_replaced` event payload so replay and
  audit remain possible. Set `False` if raw tool output must not be
  persisted.
- `ab_mode`, `ab_sample_rate`, `regression_threshold`,
  `regression_min_runs` — opt-in trust mode, covered below.

**Behaviour.**

- The summary is produced by the *same provider's* cheap tier —
  `claude-haiku-4-5` for Anthropic requests, `gpt-4o-mini` for OpenAI
  — using a client constructed on the spot. The summarising prompt
  includes the user's most recent question so the summary keeps what
  matters for the task at hand.
- If the cheap-model call fails (or the provider has no cheap tier
  configured), the policy falls back to mechanical truncation at the
  token budget with an explicit `[truncated by inkfoot]` marker —
  the request is never blocked by a summariser failure.
- Replacements are cached by content hash. Conversation history means
  the same oversized result is re-sent on every subsequent turn; the
  cache swaps in the stored summary without a second summariser call,
  so each unique result is summarised (and billed) exactly once.
- The replacement happens on the outgoing request only — your
  framework's own message history is never mutated.

**Event payload.** One `summariser_replaced` event per unique result:
`original_tokens`, `summary_tokens`, `summariser_model`, `tool_id`,
and (when `preserve_for_replay=True`) `raw`.

### Where the summariser's own cost lands

The summariser call is itself an LLM call, and it is instrumented like
any other — but its input cost is re-attributed to the ledger's
`summariser_tokens` category rather than the user-facing input
categories, so reports show "what summarisation cost" as a single
line instead of polluting `user_input_tokens`. The helper call's
output tokens stay in `output_tokens`, and the call's event carries
`metadata["summariser_call"] = true` so roll-ups can group it.

If the run's task also has a
[Token Contract](token-contracts.md#policy-helper-calls-dont-count),
the helper call doesn't consume the contract's `max_llm_calls`
budget and is never blocked by the degrade ladder — only its real
spend folds into the running total that `max_nanodollars` bounds.

### Kill switches

Two ways to turn summarisation off without redeploying:

```python
# Per run, from inside the run:
inkfoot.tag("disable_summariser", True)

# Per task, process-wide:
from inkfoot.policy.cheap_summariser import (
    disable_summariser_for_task,
    enable_summariser_for_task,
)
disable_summariser_for_task("triage")
```

When disabled, oversized results pass through to the provider
unchanged.

### Trust mode (A/B)

Summarisation trades tokens for the *risk* that the summary dropped
something the agent needed. Trust mode measures that risk on your own
traffic instead of asking you to take it on faith:

```python
CheapSummariser(ab_mode=True, ab_sample_rate=0.10)
```

- Each run is assigned a sticky branch on first eligible call:
  control (raw results, probability `ab_sample_rate`) or treatment
  (summarised). The assignment is recorded as a
  `summariser_ab_assignment` event.
- Once enough finished runs exist on both branches
  (`regression_min_runs` each, default 5), the policy compares
  success rates between branches for the same task.
- If the treatment branch's success rate drops by more than
  `regression_threshold` (default 0.05) against control,
  summarisation **auto-disables for that task**, a
  `summariser_quality_regression` event is recorded, and the
  `summariser-quality-regression` smell surfaces the finding —
  with both branches' run counts and success rates — in the next
  report. Re-enable explicitly with `enable_summariser_for_task()`
  once you've adjusted the threshold or prompt.

## Performance note

Inkfoot's [policy hooks run under a microsecond-scale budget](performance.md):
they inspect and edit dictionaries, nothing more. The one deliberate
exception is the `CheapSummariser` helper call — a real LLM
round-trip that takes as long as the cheap model takes to answer. It
fires at most once per unique oversized result (the content-hash
cache absorbs every re-send), and it is excluded from the hook
budget: you are trading one cheap-model call for that result's full
input cost on every remaining turn of the run.

## Failure isolation

Like all policies, both hooks run inside Inkfoot's
exception-isolation boundary. A modification policy that raises is
logged at `WARNING` and the request goes out exactly as your code
built it — a policy bug degrades to "no optimisation", never to a
broken agent.
