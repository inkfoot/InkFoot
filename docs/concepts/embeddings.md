# Embeddings

Retrieval-augmented and vector applications spend tokens on two very
different things: the chat/completion calls that reason over context,
and the embedding calls that turn text into vectors. Inkfoot keeps
them apart. Embedding calls are captured as their own event kind and
reported in their own section — they are **never** folded into the
causal token ledger or the headline cost of a run.

## Why a separate event kind

The [causal token ledger](causal-token-ledger.md) answers "where did
the tokens in this *reasoning* call go?" — system prompt, tool
schemas, retrieved context, and so on. An embedding call has none of
that structure: it is a flat list of input tokens with no output, no
cache tiers, and no per-category cause. Mixing embedding tokens into
the ledger would inflate `user_input_tokens` with text that was never
part of a prompt and skew every attribution percentage below it.

So embeddings get their own lane:

- They are recorded as `embedding_call` events, separate from
  `llm_call`.
- They do **not** contribute to a run's `total_input_tokens`,
  `total_nanodollars`, or the attribution bar chart.
- They surface in a dedicated "Embeddings" section in the report.

## Opt in

Embedding capture is **off by default** — most callers don't want the
extra event stream. Turn it on with a single keyword:

```python
import inkfoot

inkfoot.instrument(embeddings=True)
```

With that flag set, calls to `client.embeddings.create(...)` are
captured directly through the raw OpenAI SDK. Embedding calls made
through LangChain — `OpenAIEmbeddings`, `BedrockEmbeddings`,
`GoogleGenerativeAIEmbeddings`, `VoyageAIEmbeddings`, and the like —
are captured by wrapping the LangChain `Embeddings` classes (LangChain
has no embeddings callback, so this patches the `embed_documents` /
`embed_query` methods the way the raw shims patch a provider client).
That covers providers with no raw-SDK shim of their own. When a call
is seen by both layers — `OpenAIEmbeddings` drives the OpenAI SDK
underneath — the raw layer wins (it has the provider's exact usage)
and the duplicate is suppressed.

```python
import inkfoot
from openai import OpenAI

inkfoot.instrument(embeddings=True)

client = OpenAI()
with inkfoot.agent_run(task="index-docs"):
    client.embeddings.create(
        model="text-embedding-3-small",
        input=["chunk one", "chunk two", "chunk three"],
    )
```

Each call records the provider, model, input-token count, batch size
(the number of inputs in the call), and an estimated cost. The token
count comes from the provider's own reported usage when present and
falls back to the local tokeniser otherwise — the report flags which
calls were estimated.

## In the report

The embeddings section renders below the causal-attribution chart and
keeps its own running totals:

```console
$ inkfoot report --run run-01JX0...

Run run-01JX0... · index-docs · 4.1s · $0.0210 · success

Causal attribution:
  user_input          61.9%  ███████░░░░░  $0.0130
  output              38.1%  ████░░░░░░░░  $0.0080

Embeddings (separate accounting — not part of the ledger above):
  text-embedding-3-small  3 calls · 1,240 tokens · $0.0000
  total                   3 calls · 1,240 tokens · $0.0000
```

The `$0.0210` headline is the **chat** cost only. The embeddings line
sits beneath it with its own token and dollar totals, so the two never
get conflated.

Want the pre-embeddings view back? Pass `--exclude-embeddings` for a
chat-only report:

```bash
inkfoot report --run run-01JX0... --exclude-embeddings
```

## Pricing

Embedding models are priced from a small table separate from the chat
price list — they bill a single input rate with no output or cache
tiers. The snapshot covers the common OpenAI, Google, and Voyage AI
models. An embedding model that isn't in the table still counts toward
the token totals; its cost column shows `(unpriced)` rather than a
guessed number.

!!! note "Why doesn't this match my OpenAI bill?"
    The estimate is per-call and based on a static price snapshot, and
    it deliberately *excludes* embedding spend from your run's chat
    cost. If you're reconciling against a provider invoice, add the
    embeddings-section total to the chat total — they are reported
    separately on purpose.

## Where to next

- [The Causal Token Ledger](causal-token-ledger.md) — what the chat
  attribution actually measures, and why embeddings don't belong in
  it.
- [Accuracy & Estimation](accuracy.md) — how token counts are
  produced and when they're flagged as estimates.
