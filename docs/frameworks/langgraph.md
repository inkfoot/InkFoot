# LangGraph

Inkfoot ships a first-class LangGraph adapter that wraps your
compiled graph and produces per-node attribution. Drop in one
call after `graph.compile()` and `inkfoot report --run <id>
--group-by node` slices the cost by the LangGraph node that spent
it.

## Install

```bash
pip install "inkfoot[langgraph]"
```

The `[langgraph]` extra pulls the matching LangGraph peer
dependency. Pin a specific version of LangGraph in your own
`pyproject.toml` if you want hermetic installs; Inkfoot's
adapter is forward-compatible across LangGraph's documented
minor releases.

## Instrument

```python
import inkfoot
import inkfoot.langgraph
from langgraph.graph import StateGraph, END

inkfoot.instrument()

graph = StateGraph(MyState)
graph.add_node("retrieve", retrieve_node)
graph.add_node("synthesise", synthesise_node)
graph.add_edge("retrieve", "synthesise")
graph.add_edge("synthesise", END)

compiled = graph.compile()
inkfoot.langgraph.instrument(compiled)   # ← the one Inkfoot line
```

`inkfoot.langgraph.instrument(compiled)`:

1. Wraps `compiled.invoke` / `ainvoke` / `stream` / `astream` to
   scope a run around the whole graph execution.
2. Wraps each registered node so node entry / exit emit
   `node_enter` / `node_exit` events with the LangGraph node
   name attached as metadata.
3. Snapshots the graph's tools array at compile time so the
   resulting cost attribution lines up across runs that share
   the same tool set.

The call is idempotent — instrumenting the same compiled graph
twice is a no-op. With the adapter active, both
[modification policies](../concepts/modification-policies.md) can be
registered.

## What you get

Every LLM call inside a LangGraph run carries
`metadata.node_name = "retrieve"` (or whatever the active node
is). Two views surface this:

### Per-node cost breakdown

```bash
inkfoot report --run run-01JX0... --group-by node
```

```
Run run-01JX0... · customer-support-triage · per-node ledger

  node                     calls  input_tok  output_tok       cost
  retrieve                     1        540          85    $0.0030
  synthesise                   1       1240         230    $0.0084
```

Sorted by spend descending so the most expensive node lands at
the top.

### Smells with node provenance

Smells that fire on calls inside a specific node carry the node
name in their evidence dict. The standard
`Smells detected (...)` block in `inkfoot report --run` continues
to render normally — the node attribution lives in the
machine-readable side.

## Worked example — drive a small graph

```python
import inkfoot
import inkfoot.langgraph
from typing import TypedDict
from langgraph.graph import StateGraph, END
import anthropic

inkfoot.instrument()


class State(TypedDict):
    query: str
    chunks: list[str]
    answer: str


def retrieve(state: State) -> State:
    # Pretend we did vector retrieval.
    return {"chunks": [f"chunk for: {state['query']}"], **state}


def synthesise(state: State) -> State:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system="Answer using the chunks below.",
        messages=[
            {
                "role": "user",
                "content": f"Q: {state['query']}\nChunks: {state['chunks']}",
            }
        ],
    )
    return {**state, "answer": response.content[0].text}


graph = StateGraph(State)
graph.add_node("retrieve", retrieve)
graph.add_node("synthesise", synthesise)
graph.set_entry_point("retrieve")
graph.add_edge("retrieve", "synthesise")
graph.add_edge("synthesise", END)

compiled = graph.compile()
inkfoot.langgraph.instrument(compiled)


@inkfoot.agent_run(task="rag-qa")
def answer(query: str) -> str:
    result = compiled.invoke({"query": query, "chunks": [], "answer": ""})
    inkfoot.set_outcome("success")
    return result["answer"]


if __name__ == "__main__":
    print(answer("What does Inkfoot do?"))
```

Now `inkfoot report --run <id> --group-by node` shows the
`retrieve` row (no LLM cost — no calls inside it) and the
`synthesise` row (the entire LLM cost).

## Re-instrumenting a recompiled graph

When you recompile a graph (e.g. after editing the node set in a
notebook), call `inkfoot.langgraph.instrument(compiled)` on the
new compiled object. The previous wrapping doesn't carry over
across compiles, and the adapter is idempotent so a fresh call
on a fresh graph is fine.

## Where to next

- [Cost Smells](../concepts/cost-smells.md) — the catalogue of
  patterns the engine watches for inside your nodes.
- [OpenTelemetry](../concepts/otel.md) — the LangGraph adapter
  exports `inkfoot.run_id` and node metadata on the OTel export
  path, so dashboards downstream see the node grouping too.
- [Cost smells live alongside framework adapters in storage](../concepts/observation-policies.md) —
  policies still apply, regardless of which adapter scoped the
  run.
