# Raw Provider SDK (Anthropic / OpenAI / Gemini)

The simplest integration. You're already calling
`anthropic.Anthropic().messages.create(...)` (or the OpenAI or
Gemini equivalent), and you want Inkfoot to attribute the cost
without adopting a framework. This page covers:

- The single-line install (`inkfoot.instrument()`).
- The decorator that scopes a run (`@inkfoot.agent_run(task=...)`).
- The helpers that segment a long-running agent without a
  framework (`inkfoot.tag_node(...)`, `inkfoot.checkpoint(...)`).
- The full options on `instrument()` including the replay-mode
  capture.

## 1. Patch the SDK

```python
import inkfoot

inkfoot.instrument()
```

The call auto-detects every supported provider SDK importable in
your process and patches its client methods. Today's coverage:

| SDK | Patched methods |
|---|---|
| `anthropic` | `Anthropic().messages.create`, `AsyncAnthropic().messages.create` |
| `openai` | `OpenAI().chat.completions.create`, `AsyncOpenAI().chat.completions.create` |
| `google-generativeai` | `GenerativeModel().generate_content`, `GenerativeModel().generate_content_async` |

Bedrock and OpenAI-compatible endpoints are integrated at the
provider level rather than through a shim — see
[Providers](../providers.md) for those, the capability matrix,
and the per-provider usage-mapping notes.

Calls flowing through any patched method emit an `llm_call` event
into Inkfoot's storage. Nothing else changes — your code
continues to receive the provider's response object as-is.

## 2. Scope a run

A *run* is one unit of agent work — handle one ticket, answer one
query, process one document. Wrap each one with
`@inkfoot.agent_run(task=...)` so the LLM calls inside it land
under a single attributable record.

```python
import inkfoot
import anthropic

@inkfoot.agent_run(task="customer-support-triage")
def handle_ticket(ticket_id: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Triage ticket {ticket_id}"}],
    )
    inkfoot.set_outcome("success", quality_score=0.94)
    return response.content[0].text
```

The decorator also works as a context manager (`with
inkfoot.agent_run(task="..."):`) for non-function-shaped agent
loops.

## 3. Segment a long agent

For multi-step agents you can break the ledger into named
phases without committing to LangGraph nodes. Two helpers:

- `inkfoot.tag_node(name: str)` — every subsequent LLM call (until
  the next `tag_node`) lands under `metadata.node_name = name`.
  `inkfoot report --run <id> --group-by node` slices the bar
  chart by these names.
- `inkfoot.checkpoint(label: str)` — emits a `checkpoint` event
  so reports can show time elapsed between phase boundaries.

```python
@inkfoot.agent_run(task="invoice-extraction")
def extract(invoice_id: str) -> dict:
    inkfoot.tag_node("retrieval")
    inkfoot.checkpoint("after-fetch")
    chunks = fetch_invoice_pages(invoice_id)

    inkfoot.tag_node("synthesis")
    inkfoot.checkpoint("before-llm")
    return synthesise(chunks)
```

`inkfoot report --run <id> --group-by node` then breaks the cost
into `retrieval` vs `synthesis`, and checkpoints surface as time
markers on the run timeline.

## 4. Per-call retrieval-context attribution

When your agent pulls in retrieved context (RAG chunks, document
excerpts), wrap each addition with `inkfoot.tag_retrieval(text)`
so the tokens land in the `retrieved_context_tokens` ledger
field rather than getting bucketed under `user_input`:

```python
chunks = retriever.search(query)
for chunk in chunks:
    inkfoot.tag_retrieval(chunk.text)

# The next LLM call's retrieved_context_tokens field carries
# the sum of the tagged chunks.
client.messages.create(...)
```

## 5. The full `instrument()` surface

```python
inkfoot.instrument(
    sdks=None,                  # auto-detect, or e.g. ["anthropic"]
    policies=None,              # list of policy objects (BudgetCap, …)
    storage=None,               # default: SQLiteStorage at ~/.inkfoot/runs.db
    log_level="WARNING",        # log level for the "inkfoot" tree
    capture_mode="metadata",    # "metadata" or "replay"
    otel_export_endpoint=None,  # mirror events to an OTel collector
    otel_ingest_port=None,      # accept OTel spans on a local port
)
```

| Argument | Purpose |
|---|---|
| `sdks` | Restrict instrumentation to a subset of installed SDKs. `None` auto-detects. |
| `policies` | A list of policy objects. See [Observation Policies](../concepts/observation-policies.md). [Modification policies](../concepts/modification-policies.md) are rejected here — they need a framework adapter. |
| `storage` | Custom storage backend. Pass an explicit `SQLiteStorage(path=...)` only if you want a non-default location. |
| `log_level` | Sets the level for the `inkfoot` logger tree. Set to `"DEBUG"` while troubleshooting. |
| `capture_mode` | `"metadata"` records token counts only. `"replay"` *also* writes the full request and response bodies so future tooling can re-run the same call under different conditions. |
| `otel_export_endpoint` | See [OpenTelemetry — Export](../concepts/otel.md#export-forward-inkfoot-events-to-your-collector). |
| `otel_ingest_port` | See [OpenTelemetry — Ingest](../concepts/otel.md#ingest-point-your-collector-at-inkfoot). |

### Replay-mode capture

`capture_mode="replay"` writes the request body, response body,
and any tool-result bodies into a sibling `event_contents` table
in storage. This is what lets future tooling re-run a captured
call under different conditions (different prompts, different
models, different system blocks). The cost is disk: replay-mode
storage is ~5–20× larger than metadata-only.

Don't enable replay mode globally unless you've thought about the
privacy posture — every byte of the request and response (which
may include PII or secrets) lands in plain JSON on disk. The
[Storage & Configuration](../concepts/storage.md) page covers
the storage redaction hooks.

## Pattern-A vs Pattern-B at a glance

| Need | Pattern A (just `instrument`) | Pattern B (add `@agent_run`) |
|---|---|---|
| Single LLM call per agent invocation | ✅ enough | optional |
| Multi-call agent loop you want to attribute as one unit | ❌ — each call lands as a stand-alone run | ✅ — runs are scoped to the decorator |
| Outcome / quality scoring | requires `agent_run` for `set_outcome` | ✅ |
| Per-phase attribution (`tag_node`, `checkpoint`) | requires `agent_run` | ✅ |

Default to Pattern B unless your agent is genuinely one call long.

## Where to next

- [Providers](../providers.md) — the full provider matrix:
  capability flags, Gemini cache resources, Bedrock, and
  OpenAI-compatible endpoints.
- [LangGraph](langgraph.md) — if you'll adopt LangGraph later,
  the adapter gives you per-node attribution for free.
- [Cost Smells](../concepts/cost-smells.md) — the patterns the
  engine looks for in your runs.
- [Tracking Runs](../concepts/tracking-runs.md) — the full
  lifecycle (`agent_run` / `set_outcome` / `tag` /
  `tag_retrieval`).
