# LangChain integration model

LangChain calls can reach Inkfoot through **two** capture layers at
once, and the way they cooperate explains both the zero-config setup
and the one accuracy caveat worth knowing. This page is the
conceptual picture; the [LangChain framework page](../frameworks/langchain.md)
is the step-by-step setup.

## Two ways a LangChain call is seen

When you call `model.invoke(...)` on a `ChatAnthropic` or
`ChatOpenAI`, the work flows through two surfaces Inkfoot can watch:

- **The callback handler** sees the call the way *LangChain* sees it:
  a normalised request and a normalised `usage_metadata` block, the
  same shape regardless of which provider sits underneath.
- **The raw-SDK shim** sees the call the way the *provider* sees it:
  the actual `anthropic` / `openai` wire request and response, with
  every provider-specific field intact.

Both can be active in the same process. The handler registers
globally when you call `inkfoot.instrument()` with `langchain-core`
importable; the shim patches the provider SDK whenever it is
importable. A `ChatAnthropic` call therefore passes the handler
*and* the `anthropic` shim.

## Why that doesn't double-count

A call seen by both layers would land twice without coordination. It
doesn't, because both layers key on the **provider response id** and
deduplicate per run: the first layer to emit an event for a given id
wins, and the second sighting is dropped.

The shim fires *inside* the SDK call, so when both layers are live the
shim's event — built from the raw provider response — is the one that
survives, and the handler's later sighting is skipped. Calls with no
extractable response id are never deduplicated: Inkfoot double-counts
rather than risk dropping a real call. The full mechanics, including
the streaming and error paths, are on the
[framework page](../frameworks/langchain.md#double-capture-the-handler-and-the-raw-sdk-shims).

The practical upshot: you never choose between the handler and the
shim. Run both; you get the richer of the two events per call, once.

## The normalised-body caveat

The two layers agree on **response-side** numbers — output tokens,
cache reads, and reasoning tokens are provider-reported and identical
either way. They can differ on the **request-side split**.

The handler only ever sees LangChain's *normalised* request: a tidy
message list, normalised tool schemas, and a provider-agnostic
`usage_metadata`. That normalised body is not always byte-identical to
what the provider's API finally received — partner packages may inject
a tool-use system preamble, reserialise tool schemas, or fold options
into the request on the way down. Inkfoot's request-side attribution
(system-prompt split, tool-schema tokens, conversation memory) is
derived by re-tokenising the body it was handed, so:

- When the **raw shim also captured the call**, the surviving event is
  the shim's, built from the exact wire request. The split is as
  faithful as a raw-SDK capture.
- When **only the handler captured the call** — a provider with no
  raw-SDK shim, reached through LangChain — the split is derived from
  LangChain's normalised view. Response-side counts stay exact;
  the request-side categories are a close estimate and carry the usual
  [estimation flags](accuracy.md).

This is why the handler is a complete integration on its own, yet the
combination with the raw shims is strictly better when both apply.

## Auto-instrument vs. per-call

Global registration is the default and the right choice for almost
everyone — one `inkfoot.instrument()` captures every chain, agent, and
bare chat model in the process, across threads and async tasks, with
no `callbacks=` plumbing.

Reach for the explicit per-call handler only when you want to scope
capture to one chain rather than the whole process:

```python
from inkfoot.langchain import InkfootCallbackHandler

handler = InkfootCallbackHandler()
model.invoke("...", config={"callbacks": [handler]})
```

The same instance is safe to reuse, and it still deduplicates against
the raw shims exactly as the global handler does. Choosing per-call
capture narrows *which* calls are recorded; it does not change how a
recorded call is attributed.

## Where to next

- [LangChain framework page](../frameworks/langchain.md) — install,
  instrument, and the per-integration provider table.
- [Streaming capture](streaming.md) — how a streamed LangChain call is
  finalised, and when its usage is exact.
- [The Causal Token Ledger](causal-token-ledger.md) — what the
  request-side categories mean.
- [Accuracy & Estimation](accuracy.md) — which numbers are exact,
  which are estimated, and how flags surface in reports.
