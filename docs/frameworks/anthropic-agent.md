# Anthropic Agent SDK

Inkfoot's Anthropic Agent SDK adapter mirrors the OpenAI Agents
shape: wrap the agent's run methods + the tool-dispatch layer so
per-tool attribution is automatic.

## Install

```bash
pip install "inkfoot[anthropic-agent]"
```

The extra pulls the matching `anthropic-agent` peer dependency.
The Anthropic Agent SDK is newer than the OpenAI one — pin to a
specific version in your project until you're satisfied with how
the upstream API stabilises.

## Instrument

```python
import inkfoot
import inkfoot.anthropic_agent
from anthropic_agent import Agent

inkfoot.instrument()

agent = Agent(
    name="research-assistant",
    model="claude-sonnet-4-6",
    tools=[search, summarise],
)

inkfoot.anthropic_agent.instrument(agent)   # ← the one Inkfoot line
```

What the adapter does:

1. Scopes a `RunContext` around the agent's run and async-run
   methods so every call inside the agent loop attributes to one
   run.
2. Hooks the tool-dispatch layer; each tool invocation emits a
   `tool_dispatched` event carrying `tool_name`, `tool_args_hash`,
   `dispatch_latency_ms`.
3. Reports the Anthropic Agent SDK's policy capabilities so the
   policy registry knows what's safe to wire in — both
   [modification policies](../concepts/modification-policies.md)
   are supported.

## What you get

The same shape as the OpenAI Agents adapter: per-tool node
attribution + the 14-field ledger with `tool_schema_tokens` and
`tool_result_tokens` broken out separately. Two views worth
calling out:

### Per-tool node breakdown

```bash
inkfoot report --run run-01JX0... --group-by node
```

The Anthropic Agent adapter sets `metadata.node_name` to the
tool name on every LLM call inside a tool dispatch, so the
per-node table buckets cost by which tool drove the call.

### Cache-friendly system blocks

Anthropic's prompt cache fires hard on Claude Haiku / Sonnet when
the system block stays byte-identical. Two related smells —
[`unstable-prompt-prefix`](../concepts/cost-smells.md#unstable-prompt-prefix)
and
[`recurring-cache-writes`](../concepts/cost-smells.md#recurring-cache-writes) —
fire most often on this provider. The cache numbers come
straight from the Anthropic `usage` block, so once you stabilise
the prefix the savings show up immediately in `cache_read_tokens`.

## Worked example

```python
import inkfoot
import inkfoot.anthropic_agent
from anthropic_agent import Agent, tool

inkfoot.instrument()


@tool
def search(query: str) -> str:
    return f"results for: {query}"


@tool
def summarise(text: str) -> str:
    return text[:200]


agent = Agent(
    name="research-assistant",
    model="claude-sonnet-4-6",
    system="You research questions using the provided tools.",
    tools=[search, summarise],
)
inkfoot.anthropic_agent.instrument(agent)


@inkfoot.agent_run(task="research-question")
def research(question: str) -> str:
    result = agent.run(question)
    inkfoot.set_outcome("success")
    return result.output_text


if __name__ == "__main__":
    print(research("What's the smallest prime greater than 100?"))
```

`inkfoot report --run <id> --group-by node` shows one row per
tool; `inkfoot report --run <id>` shows the full ledger.

## Where to next

- [OpenAI Agents SDK](openai-agents.md) — the same shape on the
  OpenAI side.
- [Cost Smells](../concepts/cost-smells.md) — the catalogue,
  including the two cache-related smells that fire most often on
  Claude.
- [Spot cache-miss patterns](../recipes/spot-cache-misses.md) —
  recipe for the most common Anthropic-side cost surprise.
