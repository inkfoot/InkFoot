# Streaming capture

Streamed calls are captured exactly like buffered ones — there is no
flag to set and no separate code path for you to write. Whether you
pass `stream=True` to `create(...)`, use the ergonomic helpers
(`anthropic` `messages.stream(...)`, OpenAI `responses.stream(...)` or
`chat.completions.stream(...)`), or iterate with `for` or `async for`,
Inkfoot tees the stream and emits one `llm_call` event when you finish
draining it. The chunks you receive are passed through untouched —
same objects, same order — so instrumentation never changes what your
code sees.

## One event, finalised at close

A buffered call is complete the moment it returns. A streamed call is
only complete once the **stream closes**, which is later — sometimes
much later. Inkfoot finalises the event when:

- you finish iterating the stream, or
- its context manager exits (`with client.responses.stream(...) as s`),
  or
- the stream object is garbage-collected, for a partly-consumed or
  abandoned stream.

So a streamed call's event timestamp tracks when the *caller* was done
with it, not when the first byte arrived. For a run that opens a
stream and drains it later, the cost still attributes to the run that
created it.

## Exact vs. estimated output

The output-token count on a streamed call is **exact** when the
stream carries terminal usage, and a **tokeniser estimate** when it
doesn't. Which one you get depends on the surface:

| Surface | Terminal usage on the stream? | How to get exact output |
|---|---|---|
| Anthropic (`messages.stream` / `stream=True`) | Yes — `message_delta` carries it | automatic |
| OpenAI Responses (`responses.stream` / `stream=True`) | Yes — on the `response.completed` event | automatic |
| OpenAI Chat Completions (`chat.completions` streamed) | Only if you opt in | pass `stream_options={"include_usage": True}` |

Request-side attribution (the system / user / tool-schema split) is
unaffected by streaming — it's derived from the request you sent,
which is fully known before the first chunk. Only the *output* count
depends on terminal usage.

## The OpenAI Chat opt-in

OpenAI's Chat Completions stream omits usage unless you ask for it.
Without the opt-in, Inkfoot reconstructs the assistant text from the
streamed deltas, counts it with the local tokeniser, and flags the
event `stream_options_off` so the report shows the output count is
estimated:

```python
# Exact output tokens on a streamed Chat Completions call:
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "…"}],
    stream=True,
    stream_options={"include_usage": True},  # ← the opt-in
)
for chunk in stream:
    ...
```

There is no process-wide switch for this — `stream_options` is a
per-call argument of the OpenAI SDK. If your calls go through a
wrapper, set it once in the wrapper's default keyword arguments so
every streamed Chat call carries it.

Any other streamed surface that closes without terminal usage falls
back to the same estimate, flagged with the generic `stream_no_usage`.
Either flag means *output tokens were estimated*; request-side and
pricing are otherwise normal.

## Streaming through LangChain

The LangChain callback handler reads whatever `usage_metadata` the
integration attaches at the end of the stream. Integrations that omit
usage on streamed responses produce `usage_metadata_missing` events
through the handler alone. When the raw-SDK shim is also active it
captures the streamed call directly — with exact output whenever the
stream carries terminal usage — and the
[dedup](langchain-integration.md#why-that-doesnt-double-count) keeps
the shim's richer event, sidestepping the gap. Running both layers is
the recommended setup for exactly this reason.

## Where to next

- [Spot streaming-cost surprises](../recipes/streaming-cost-surprises.md)
  — diagnose a `stream_options_off` flag end to end.
- [Accuracy & Estimation](accuracy.md) — how estimation flags surface
  in `inkfoot report`.
- [OpenAI Responses API](openai-responses.md) — the surface whose
  streams carry usage for free.
