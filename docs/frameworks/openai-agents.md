# OpenAI Agents SDK

Inkfoot's OpenAI Agents SDK adapter wraps `Agent.run` and the
tool-dispatch layer, so per-tool cost attribution comes naturally
from the SDK's own tool names.

## Install

```bash
pip install "inkfoot[openai-agents]"
```

The extra pulls the matching `openai-agents` peer dependency.

## Instrument

```python
import inkfoot
import inkfoot.openai_agents
from openai_agents import Agent

inkfoot.instrument()

agent = Agent(
    name="customer-support",
    model="gpt-4o-mini",
    tools=[lookup_order, refund_charge, escalate_to_human],
)

inkfoot.openai_agents.instrument(agent)   # ← the one Inkfoot line
```

`inkfoot.openai_agents.instrument(agent)`:

1. Scopes a `RunContext` around `agent.run` and `agent.run_async`
   so every call inside the agent loop attributes to one run.
2. Hooks the tool-dispatch layer so each tool invocation emits a
   `tool_dispatched` event carrying `tool_name`, `tool_args_hash`,
   and `dispatch_latency_ms`.
3. Reports the OpenAI Agents SDK's policy capabilities to the
   policy registry, so policy plumbing knows what's safe to
   register — both
   [modification policies](../concepts/modification-policies.md)
   are supported.

## What you get

### Per-tool dispatch records

```bash
inkfoot report --run run-01JX0... --group-by node
```

The OpenAI Agents adapter populates `node_name` with the tool
name on every tool-dispatched LLM call, so the per-node view
slices by tool. Useful for "which tool eats the most input
tokens?" — typically your retrieval / lookup tool.

### Cost attribution with tool schemas isolated

The 14-field ledger keeps `tool_schema_tokens` separate from
`tool_result_tokens` and the rest of `user_input`. Once you have
the OpenAI Agents adapter installed, the bar chart on a typical
multi-tool run looks roughly like:

```
Causal attribution:
  tool_schema         32%  ████░░░░░░░░  $0.0094
  tool_result         28%  ███░░░░░░░░░  $0.0082
  system_static       18%  ██░░░░░░░░░░  $0.0053
  user_input          12%  █░░░░░░░░░░░  $0.0035
  output              10%  █░░░░░░░░░░░  $0.0030
```

`tool_schema_tokens` is the serialised tool array sent to the
provider on every turn. If that row is large, consider whether
you need every tool on every call, or whether a smaller tool set
keyed off the current state would do.

## Worked example

```python
import inkfoot
import inkfoot.openai_agents
from openai_agents import Agent, tool

inkfoot.instrument()


@tool
def lookup_order(order_id: str) -> dict:
    # Pretend this hits a database.
    return {"order_id": order_id, "status": "shipped"}


@tool
def refund_charge(charge_id: str, amount_cents: int) -> dict:
    return {"refund_id": "rfnd-x", "amount_cents": amount_cents}


agent = Agent(
    name="support",
    model="gpt-4o-mini",
    tools=[lookup_order, refund_charge],
    instructions="You handle customer support tickets.",
)
inkfoot.openai_agents.instrument(agent)


@inkfoot.agent_run(task="customer-support-triage")
def handle(query: str) -> str:
    result = agent.run(query)
    inkfoot.set_outcome("success", quality_score=0.92)
    return result.output_text


if __name__ == "__main__":
    print(handle("Where's order 9981?"))
```

After one call, `inkfoot report --run <id> --group-by node`
shows per-tool attribution; `inkfoot report --run <id>` shows the
14-field ledger with `tool_schema_tokens` and `tool_result_tokens`
broken out.

## Async + streaming

The adapter wraps `Agent.run_async` symmetrically — no extra
work for async agents. Streaming responses
(`Agent.run_stream(...)`) emit one `llm_call` event per logical
call once the stream completes, so the report still attributes
correctly even when your code yields partials.

## Where to next

- [Cost Smells](../concepts/cost-smells.md) — the OpenAI Agents
  adapter doesn't change which smells fire, just enriches the
  attribution they reference.
- [Find your most expensive agent](../recipes/find-expensive-agent.md) —
  the aggregate view is the same regardless of adapter; the
  per-tool breakdown only changes what you see in the drill-in.
- [Anthropic Agent SDK](anthropic-agent.md) — symmetric
  adapter for the Anthropic side.
