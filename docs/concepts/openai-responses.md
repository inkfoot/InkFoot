# OpenAI Responses API

OpenAI ships two chat surfaces: the original **Chat Completions** API
(`client.chat.completions.create`) and the newer **Responses** API
(`client.responses.create`). Inkfoot captures both. They install
together — one `inkfoot.instrument()` patches `chat.completions.create`
and `responses.create` whenever `openai` is importable — so you don't
choose an integration; you just call whichever API your code uses.

```python
import inkfoot
from openai import OpenAI

inkfoot.instrument()

client = OpenAI()
with inkfoot.agent_run(task="triage"):
    response = client.responses.create(
        model="gpt-4o",
        instructions="You triage customer-support tickets.",
        input="Triage ticket TKT-1234.",
    )
```

That call lands as the same fully attributed `llm_call` event a Chat
Completions call would, priced on the same OpenAI rows.

## When to use which

The two APIs are priced identically, so the choice is ergonomic, not
about cost:

- **Chat Completions** is the established surface — the broadest
  ecosystem support, and what most existing code already calls.
- **Responses** is OpenAI's forward path: a single `instructions`
  field for the system prompt, server-managed conversation state, and
  first-class reasoning output for the reasoning models.

If you're on Azure, both surfaces route through the same client
classes, so Azure calls on either API are captured with no extra
setup.

## Shape mapping

The Responses API renames nearly every field the Chat Completions
attribution keys on, so Inkfoot translates it with a dedicated recipe.
The mapping, field for field:

| What it is | Chat Completions | Responses API |
|---|---|---|
| System prompt | `messages[role="system"]` | top-level `instructions` (plus any `system` / `developer` items in `input`) |
| User / turn input | `messages[role="user"]` | `input` — a plain string, or a list of typed items |
| Tool schemas | `{"type": "function", "function": {…}}` | flat `{"type": "function", "name": …, "parameters": …}` |
| Tool results | `messages[role="tool"]` | `function_call_output` items in `input` |
| Tool calls returned | `choices[0].message.tool_calls` | `output[]` items with `type="function_call"` |
| Input tokens | `usage.prompt_tokens` | `usage.input_tokens` |
| Output tokens | `usage.completion_tokens` | `usage.output_tokens` |
| Cache reads | `usage.prompt_tokens_details.cached_tokens` | `usage.input_tokens_details.cached_tokens` |
| Reasoning tokens | `usage.completion_tokens_details.reasoning_tokens` | `usage.output_tokens_details.reasoning_tokens` |

Once translated, both surfaces feed the identical
[Causal Token Ledger](causal-token-ledger.md): the `instructions`
string is split into static vs. dynamic system prompt the same way a
Chat Completions system message is, the typed `input` items become the
latest user turn plus conversation memory, and reasoning tokens are
broken out from output.

## Forgiving by design

The Responses surface is still growing. Rather than break when OpenAI
adds a field, the translator is deliberately lenient: a response
carrying a top-level key Inkfoot hasn't mapped yet is **flagged**
(`responses_shape_unknown:<key>`) and translated anyway — never
dropped, never raised on. The known counts still land; the flag tells
you a newer response shape went partially unmodelled, which is the
cue to upgrade Inkfoot.

## Streaming

`client.responses.create(stream=True)` and the
`client.responses.stream(...)` helper are captured like any other
streamed call: Inkfoot tees the stream and emits one event when you
finish draining it. Responses streams carry terminal usage on the
`response.completed` event, so a streamed Responses call reports exact
output tokens without any extra options. See
[Streaming capture](streaming.md) for the full picture.

## Where to next

- [Streaming capture](streaming.md) — when a streamed call's usage is
  exact and when it's estimated.
- [The Causal Token Ledger](causal-token-ledger.md) — the categories
  both OpenAI surfaces feed.
- [Raw Provider SDK](../frameworks/raw-sdk.md) — the full
  `instrument()` walkthrough for the OpenAI and Anthropic shims.
