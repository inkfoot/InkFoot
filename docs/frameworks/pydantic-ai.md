# Pydantic AI

Inkfoot's Pydantic AI adapter wraps `Agent.run` / `Agent.run_sync`
and the registered-tool layer, so each agent loop becomes one run
and every tool invocation lands as a dispatch event next to the
`llm_call` events the provider shim records underneath.

## Install

```bash
pip install "inkfoot[pydantic-ai]"
```

The extra pulls the matching `pydantic-ai` peer dependency.

## Instrument

```python
import inkfoot
import inkfoot.pydantic_ai
from pydantic_ai import Agent

inkfoot.instrument()

agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt="You handle customer support tickets.",
)

inkfoot.pydantic_ai.instrument(agent)   # ← the one Inkfoot line
```

`inkfoot.pydantic_ai.instrument(agent)`:

1. Scopes a run around `agent.run` (async) and `agent.run_sync`
   (sync) so every provider call inside the agent loop attributes
   to one run. Calls made inside an outer `inkfoot.agent_run`
   block join that run instead of opening a second one.
2. Hooks the registered-tool layer (the agent's `name → Tool`
   registry) so each tool invocation emits a `tool_dispatched`
   event carrying `tool_name`, `tool_args_hash`, and
   `dispatch_latency_ms`.
3. Reports the adapter's policy capabilities to the policy
   registry — both
   [modification policies](../concepts/modification-policies.md)
   are supported.

## What you get

### One event per agent step

Pydantic AI runs a model-call → tool-call loop until the agent
produces a final result. With the adapter installed, each
iteration shows up as an `llm_call` event (from the provider shim)
plus a `tool_dispatched` event for the tool the model picked — so
the run timeline reads exactly like the loop executed.

### Cost attribution with tool schemas isolated

The 14-field ledger keeps `tool_schema_tokens` separate from
`tool_result_tokens` and the rest of `user_input`. Pydantic AI
serialises every registered tool's JSON schema into each request,
so on tool-heavy agents the `tool_schema` row is often the first
thing worth optimising.

## Worked example

```python
import inkfoot
import inkfoot.pydantic_ai
from pydantic_ai import Agent

inkfoot.instrument()

agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt="You answer weather questions.",
)


@agent.tool_plain
def get_weather(city: str) -> dict:
    # Pretend this hits a weather API.
    return {"city": city, "forecast": "sunny", "high_c": 23}


inkfoot.pydantic_ai.instrument(agent)


@inkfoot.agent_run(task="weather-helper")
def handle(question: str) -> str:
    result = agent.run_sync(question)
    inkfoot.set_outcome("success", quality_score=0.9)
    return result.output


if __name__ == "__main__":
    print(handle("What's the weather in Tokyo?"))
```

After one call, `inkfoot report --run <id>` shows the 14-field
ledger with `tool_schema_tokens` and `tool_result_tokens` broken
out, and the run's event log carries one `tool_dispatched` row per
`get_weather` call.

## Async + streaming

`Agent.run` (the async entry point) is wrapped symmetrically — no
extra work for async agents. `Agent.run_stream` is **not**
wrapped: it returns an async context manager rather than a
coroutine, so a naive wrap would close the run before the stream
is consumed. Streaming calls still attribute correctly when you
scope them yourself:

```python
async with inkfoot.agent_run(task="weather-helper"):
    async with agent.run_stream("What's the weather?") as stream:
        async for chunk in stream.stream_text():
            ...
```

## Where to next

- [Modification policies](../concepts/modification-policies.md) —
  both `LazyToolExposure` and `CheapSummariser` register cleanly
  against this adapter.
- [Cost Smells](../concepts/cost-smells.md) — the adapter doesn't
  change which smells fire, just enriches the attribution they
  reference.
- [CrewAI](crewai.md) — multi-agent crews with per-agent and
  per-task cost slices.
