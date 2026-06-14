# Recipe: Spot streaming-cost surprises

A report's output-token count looks too round, or a CI diff flags an
output-token swing you can't explain. The usual culprit: a streamed
OpenAI Chat Completions call that never carried usage, so the output
was *estimated* rather than provider-reported. This recipe finds the
flag, confirms it, and fixes it. Target: under ten minutes.

## What you'll need

- Inkfoot installed with a few recorded runs that make streamed calls.
- The CLI on your `$PATH`: `which inkfoot` should return a path.

## 1. Spot the flag

`inkfoot report` prints an estimation-flags footer whenever any call
in the run had an approximated number:

```bash
inkfoot report --run run-01JX8...
```

```
Run run-01JX8... · chat-assistant · 2.2s · $0.0041 · success

Causal attribution:
  system_static       44.0%  █████░░░░░░░  $0.0018
  user_input          28.0%  ███░░░░░░░░░  $0.0011
  output              28.0%  ███░░░░░░░░░  $0.0012

Estimation flags:
  · stream_options_off (1 call) — output tokens estimated from streamed text
```

`stream_options_off` means exactly one thing: an OpenAI Chat
Completions call was streamed *without*
`stream_options={"include_usage": True}`, so OpenAI sent no usage on
the stream and Inkfoot counted the assistant text with its own
tokeniser. The dollar figure for that call's output is an estimate,
not a billed number.

!!! note "Estimated, not wrong"
    The tokeniser estimate is typically within a couple of percent of
    the provider's count. The flag isn't an error — it's Inkfoot
    telling you which numbers it approximated, so a CI cost diff can't
    quietly attribute a tokeniser wobble to a real regression. See
    [Accuracy & Estimation](../concepts/accuracy.md).

## 2. Confirm the call

Find the streamed call in your code — it's the one passing
`stream=True` (or using `client.chat.completions.stream(...)`) on the
OpenAI Chat surface without the usage option:

```python
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    stream=True,             # streamed …
    # stream_options missing → no usage on the stream → estimated output
)
for chunk in stream:
    ...
```

## 3. Fix it — ask for usage

Add `stream_options={"include_usage": True}`. OpenAI then sends a
final usage chunk and Inkfoot records the **exact** output count:

```python
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    stream=True,
    stream_options={"include_usage": True},   # ← the fix
)
for chunk in stream:
    ...
```

There's no global switch — `stream_options` is per call. If your
streamed calls funnel through one wrapper, set it in that wrapper's
default keyword arguments so every streamed Chat call carries it.

## 4. Verify the flag is gone

Re-run and check the footer:

```bash
inkfoot report --run run-01JX9...
```

A clean run prints no estimation-flags footer at all. The output line
now reflects OpenAI's billed token count.

## Other surfaces are already exact

You only need this on the OpenAI **Chat Completions** surface. Other
streamed calls carry terminal usage on the wire, so their output is
exact with no options:

- **Anthropic** streams (`messages.stream` / `stream=True`) — usage
  arrives on the closing `message_delta`.
- **OpenAI Responses** streams (`responses.stream` / `stream=True`) —
  usage arrives on `response.completed`.

Streaming through **LangChain**? When the raw-SDK shim is active it
captures the streamed call directly and the
[dedup](../concepts/langchain-integration.md#why-that-doesnt-double-count)
keeps that exact event over the handler's — another reason to run both
layers. The full model is on the
[Streaming capture](../concepts/streaming.md) page.

## Next step

Make the flag a tripwire, not a surprise: a CI cost diff fails loudly
when a new estimation flag appears in a pull request.

→ [Wire CI cost review for a LangChain repo](ci-cost-review-langchain.md)
