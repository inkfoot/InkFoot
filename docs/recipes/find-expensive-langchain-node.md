# Recipe: Find your most expensive LangChain node

Your agent is a LangGraph graph — a handful of nodes wired together —
and the run cost is creeping up. Which node is responsible? This
recipe instruments the graph, breaks one run down by node, and drills
into the worst offender. Target: under ten minutes.

## What you'll need

- A LangGraph agent. (`pip install "inkfoot[langchain,langgraph]"`
  plus your provider partner package.)
- The CLI on your `$PATH`: `which inkfoot` should return a path.

## 1. Instrument the graph

Two lines. `inkfoot.instrument()` registers the LangChain handler that
captures every model call; `inkfoot.langgraph.instrument(compiled)`
wraps the compiled graph so each captured call is stamped with the
node that made it.

```python title="agent.py"
import inkfoot
import inkfoot.langgraph
from langgraph.graph import StateGraph, END

inkfoot.instrument()

graph = StateGraph(MyState)
graph.add_node("retrieve", retrieve_node)
graph.add_node("rerank", rerank_node)
graph.add_node("synthesise", synthesise_node)
graph.set_entry_point("retrieve")
graph.add_edge("retrieve", "rerank")
graph.add_edge("rerank", "synthesise")
graph.add_edge("synthesise", END)

compiled = graph.compile()
inkfoot.langgraph.instrument(compiled)   # ← stamps node names


@inkfoot.agent_run(task="rag-qa")
def answer(query: str) -> str:
    result = compiled.invoke({"query": query})
    inkfoot.set_outcome("success")
    return result["answer"]
```

Run the agent once so there's a run to inspect:

```bash
python -c "import agent; print(agent.answer('What changed in the deploy?'))"
```

## 2. Break the run down by node

Grab the run id from the output (or `inkfoot report --last 1h`), then
group the single-run view by node:

```bash
inkfoot report --run run-01JX7... --group-by node
```

```
Run run-01JX7... · rag-qa · per-node ledger

  node                     calls  input_tok  output_tok       cost
  synthesise                   1       4820         310    $0.0192
  rerank                       4        980          40    $0.0061
  retrieve                     1          0           0    $0.0000
```

The table is sorted by spend, so the most expensive node is on top.
`retrieve` did no LLM work (it's vector search); `synthesise` is the
clear cost centre, and `rerank` ran four model calls.

!!! note "Where do the node names come from?"
    The LangGraph adapter stamps `metadata.node_name` on every call
    made inside a node, and `--group-by node` buckets on it. The
    LangChain handler captures the calls; the adapter attributes them.
    No adapter and no LangGraph? `inkfoot.tag_node("name")` stamps the
    same field by hand — see
    [Raw Provider SDK](../frameworks/raw-sdk.md#3-segment-a-long-agent).

## 3. Drill into the worst node

Render the full single-run view to see *where inside* the node the
money went:

```bash
inkfoot report --run run-01JX7...
```

```
Run run-01JX7... · rag-qa · 6.1s · $0.0253 · success

Causal attribution:
  retrieved_context   62.4%  ███████░░░░░  $0.0158  ⚠ oversized
  system_static       18.1%  ██░░░░░░░░░░  $0.0046
  output              12.3%  █░░░░░░░░░░░  $0.0031
  ...

Smells detected (1):
  · oversized-tool-result-recycled  (oversized)
    → Summarise large tool results before recycling them across turns.
```

More than half of `synthesise`'s cost is `retrieved_context` — the
node is stuffing every retrieved chunk into the prompt. The named
smell tells you the fix.

## 4. Decide what to change

The two most common node-level cost shapes:

- **One node dominates because of what it puts in the prompt** —
  follow the smell. Here, summarise or trim retrieved context before
  the `synthesise` call, or rerank harder upstream so fewer chunks
  reach it.
- **A node runs more model calls than you expected** — `rerank`'s
  four calls above. Check whether the loop can batch, cache, or demote
  to a cheaper model. The [Cost Smells](../concepts/cost-smells.md)
  catalogue names the policy that mechanises each fix.

## 5. Verify across more runs

One run is an anecdote. Re-run the node breakdown on a few more runs
of the same task, or watch the aggregate task view tighten after you
ship the change:

```bash
inkfoot report --last 24h --group-by task --task rag-qa
```

If `cost/success` drops and the smell stops firing, the node fix
landed.

## Next step

Once you know the worst node, stop the next regression from reaching
it silently:

→ [Wire CI cost review for a LangChain repo](ci-cost-review-langchain.md)
