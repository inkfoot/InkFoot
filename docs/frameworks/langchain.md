# LangChain

Inkfoot ships a LangChain callback handler that captures every
chat-model call made through LangChain — and through anything
built on it — as a fully attributed `llm_call` event. One
`inkfoot.instrument()` at startup is the whole integration: the
handler registers itself process-wide, so chains, agents, and
LCEL pipelines need no `callbacks=` plumbing at all.

Because the handler reads LangChain's *normalised*
`usage_metadata`, a single integration covers every chat
integration LangChain does:

| Integration | Provider tag | Notes |
|---|---|---|
| `ChatAnthropic` | `anthropic` | Cache read/creation tokens from `input_token_details` |
| `ChatOpenAI` (Chat Completions) | `openai` | Cached prompt tokens from `input_token_details.cache_read` |
| `ChatOpenAI` (`use_responses_api=True`) | `openai` | Reasoning tokens from `output_token_details.reasoning` |
| `AzureChatOpenAI` | `openai` | The `azure_openai` identifier maps onto OpenAI pricing |
| `ChatGoogleGenerativeAI` | `gemini` | Context-cache reads from `input_token_details.cache_read` |
| `ChatBedrock` / `ChatBedrockConverse` | `bedrock` | Prompt-cache details on Anthropic-family models |

Older integration versions that predate `usage_metadata` still
work: the handler falls back to the legacy
`response_metadata.token_usage` counters (`prompt_tokens` /
`completion_tokens`).

## Install

```bash
pip install "inkfoot[langchain]"
```

The `[langchain]` extra pins only `langchain-core` — the package
that defines `BaseCallbackHandler` and the normalised usage
shapes. Your per-provider partner packages
(`langchain-anthropic`, `langchain-openai`,
`langchain-google-genai`, `langchain-aws`, …) stay yours to
choose and version.

## Instrument

```python
import inkfoot

inkfoot.instrument()
```

That's it. `instrument()` defaults to `langchain="auto"`: when
`langchain-core` is importable, the handler is created once and
registered globally through LangChain's configure-hook mechanism,
so every subsequent chat-model invocation in the process — across
threads and async contexts — reports to Inkfoot.

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-haiku-4-5")

with inkfoot.agent_run(task="triage"):
    model.invoke("Summarise this ticket: ...")
```

The explicit forms:

```python
inkfoot.instrument(langchain=True)    # require it; ImportError if
                                      # langchain-core is missing
inkfoot.instrument(langchain=False)   # never register the handler
```

If you prefer per-chain wiring over global registration, the
handler is also a plain LangChain callback handler:

```python
from inkfoot.langchain import InkfootCallbackHandler

handler = InkfootCallbackHandler()
model.invoke("...", config={"callbacks": [handler]})
```

`inkfoot.shutdown()` (and process exit) deactivates the global
handler; LangChain has no unregister API, so the handler flips to
an inert no-op instead.

## What you get

Each captured call carries the same
[14-field Causal Token Ledger](../concepts/causal-token-ledger.md)
the raw-SDK shims produce:

- **Request side** — the message list is split causally:
  system prompt (static vs. dynamic via stable-prefix
  detection), the latest user turn, prior turns as conversation
  memory, embedded tool results, and the serialised tool
  schemas the model was offered.
- **Response side** — provider-reported `output_tokens`,
  cache read/creation tokens, and reasoning tokens, straight
  from `usage_metadata`. Cache status (`hit` / `miss` /
  `partial` / `n/a`) is derived from the cache token details.
- **Pricing** — the provider identifier LangChain reports
  (`anthropic`, `openai`, `azure_openai`, `google_genai`,
  `google_vertexai`, `amazon_bedrock`, `bedrock_converse`, …)
  is mapped onto Inkfoot's pricing providers, so
  `estimated_nanodollars` lands without configuration. When the
  identifier is missing entirely, a conservative sniff of the
  model name fills it in.

Events captured by the handler carry
`metadata.captured_by = "langchain_handler"` plus the LangChain
run id, so reports can distinguish them from raw-SDK shim
captures.

When a response arrives with no usage information at all, the
event still lands — flagged `usage_metadata_missing`, with the
request-side split intact — rather than silently dropping the
call. See [Accuracy & Estimation](../concepts/accuracy.md) for
how flagged events are reported.

## Double capture: the handler and the raw-SDK shims

`ChatAnthropic` ultimately calls the `anthropic` SDK — which
Inkfoot's raw-SDK shim may *also* be patching. Without care,
one provider call would land twice.

Inkfoot deduplicates on the **provider response id**, per run:
the first capture layer to emit wins, and the second sighting of
the same response id is skipped (with a DEBUG log on the
`inkfoot.shims` logger). The shim fires inside the SDK call
itself, so when both layers are active the shim's event — built
from the raw provider response — is the one that survives. Calls
with no extractable response id are never deduplicated: Inkfoot
fails open and double-counts rather than drops.

The rule covers every shimmed surface, including the OpenAI
Responses API: a `ChatOpenAI(use_responses_api=True)` call is
observed by both the Responses shim and the handler under the
same `resp_...` wire id, and collapses to the shim's event.

Failed calls have no response id, so the error path keys on the
exception itself: the shim records the exception object it caught,
and the handler's later sighting of the same exception — or of a
partner-package wrapper whose `__cause__`/`__context__` chain
contains it — is skipped. A failure raised above the SDK (tool
binding, a partner-package bug) never reaches the shim, so the
handler records it. Either way, exactly one error event lands.

The practical upshot: running the handler alongside the shims is
safe and requires no configuration.

## Limitations

- **Chat and completion models only.** Embedding calls are not
  captured yet; the handler's embeddings hooks exist but are
  inert.
- **Streaming usage depends on the integration.** The handler
  reads whatever `usage_metadata` the integration attaches at
  the end of the stream; integrations that omit usage on
  streamed responses produce `usage_metadata_missing` events.
- **Token splits are estimates.** Request-side categories are
  tokeniser-derived and carry per-category estimation flags;
  provider-side harness overhead (tool-use system prompts and
  the like) is invisible to the split. Response-side counts are
  provider-reported and exact.

## Where to next

- [The Causal Token Ledger](../concepts/causal-token-ledger.md) —
  what the 14 categories mean and the invariant they satisfy.
- [LangGraph](langgraph.md) — per-node attribution for compiled
  graphs; the callback handler captures the calls, the adapter
  attributes them to nodes.
- [Accuracy & Estimation](../concepts/accuracy.md) — which
  numbers are exact, which are estimated, and how flags surface
  in reports.
