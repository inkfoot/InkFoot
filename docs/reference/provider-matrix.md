# Provider capability matrix

Every provider integration declares a `Capabilities` record — what
the API surface supports and how Inkfoot should price and attribute
it. Policies and pricing consult these declarations at runtime: the
[`CheapSummariser`](../concepts/modification-policies.md#cheapsummariserthreshold_tokens1500)
picks its summary model from `Cheap summariser model` (and falls
back to mechanical truncation where there is none), and cache reads
and writes are priced by multiplying the base input-token rate by
the cache price ratios.

This table is CI-checked against the live provider declarations —
a drifted cell fails the test suite.

| Capability | Anthropic | OpenAI | Gemini | Bedrock (Anthropic models) | Bedrock (other families) | OpenAI-compatible (default) |
|---|---|---|---|---|---|---|
| Provider string | `anthropic` | `openai` | `gemini` | `bedrock` | `bedrock` | `openai_compat` |
| Tool use | yes | yes | yes | yes | yes | yes |
| Image input | yes | yes | yes | yes | no | no |
| Document blocks | yes | no | yes | yes | no | no |
| Prompt caching | yes | yes | yes | yes | no | no |
| Cache style | `explicit_marker` | `automatic` | `cache_resource` | `explicit_marker` | `none` | `none` |
| Cache read price ratio | 0.1 | 0.5 | 0.25 | 0.1 | 1.0 | 1.0 |
| Cache write price ratio | 1.25 | 0.0 | 1.0 | 1.25 | 1.0 | 1.0 |
| JSON response format | no | yes | yes | no | no | no |
| Cheap summariser model | `claude-haiku-4-5` | `gpt-4o-mini` | `gemini-1.5-flash` | `anthropic.claude-3-5-haiku-20241022-v1:0` | — | — |

## Reading the rows

- **Provider string** — the value recorded in every `llm_call`
  event's `provider` field, and the name used in reports and OTel
  attributes.
- **Tool use** — the provider accepts a tool/function schema array
  on the request. All six columns support it, which is why
  [`LazyToolExposure`](../concepts/modification-policies.md#lazytoolexposurestale_after_turns3-core_tools)
  works across the board.
- **Image input / Document blocks** — whether image and document
  content blocks are accepted and attributed to their own ledger
  categories.
- **Prompt caching / Cache style** — how the provider exposes
  prompt caching: `explicit_marker` (cache breakpoints set in the
  request), `automatic` (the provider caches transparently and
  reports hits in usage), `cache_resource` (a cached-content
  resource is created and referenced), or `none`.
- **Cache read / write price ratio** — multipliers applied to the
  base input rate when pricing cached tokens. A read ratio of `0.1`
  means cache hits bill at 10% of the normal input price; a write
  ratio of `1.25` means populating the cache costs 25% over the
  normal rate. Where caching is unsupported, both ratios are `1.0`
  and never apply.
- **JSON response format** — the provider accepts a structured
  `response_format`-style request parameter.
- **Cheap summariser model** — the same-provider cheap tier
  `CheapSummariser` targets. Where this is `—` the policy never
  makes a helper call; oversized tool results degrade to mechanical
  truncation instead.

## Bedrock resolves per model family

Bedrock is a multi-vendor gateway, so its capabilities are resolved
from the model id's prefix at call time:

| Model id prefix | Column that applies |
|---|---|
| `anthropic.` | Bedrock (Anthropic models) |
| `meta.llama`, `amazon.titan`, `mistral.`, `cohere.` | Bedrock (other families) |

Unrecognised families get the conservative "other families" column:
tool use only, no caching, no cheap summary tier.

The same family resolution applies to Claude reached through
Anthropic's `AnthropicBedrock` client, which the Anthropic shim
captures and tags `provider="anthropic_bedrock"`. That string resolves
to the same Bedrock Anthropic-models column above (and the same
Bedrock pricing rows), so it shares the explicit-marker caching style
and a Bedrock-namespaced cheap summariser model — distinct from the
direct `anthropic` provider's.

## OpenAI-compatible endpoints are declared per instance

`OpenAICompatProvider` covers self-hosted and third-party endpoints
that speak the OpenAI wire format (Ollama, vLLM, llama.cpp,
Together, …). Those backends differ wildly, so the defaults above
are deliberately conservative — and you can override any field for
the endpoint you actually run:

```python
from inkfoot.providers.openai_compat import OpenAICompatProvider

provider = OpenAICompatProvider(
    base_url="https://my-gateway.internal/v1",
    model="llama3.2",
    capabilities={
        "supports_prompt_cache": True,
        "prompt_cache_style": "automatic",
        "cache_read_price_ratio": 0.5,
    },
)
```

Overrides are validated at construction, so an incoherent
declaration (say, a cache style without cache support) fails loudly
instead of misleading policies later. See
[Providers](../providers.md) for wiring an instance into your
instrumentation.
