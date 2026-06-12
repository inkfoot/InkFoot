# Providers

Inkfoot ships five provider integrations: Anthropic, OpenAI,
Google Gemini, Amazon Bedrock, and a single OpenAI-compatible
class that covers the long tail of endpoints speaking the OpenAI
wire protocol (vLLM, Together, Fireworks, Groq, LM Studio,
Ollama, …).

Each provider declares its variance — caching style, cache price
ratios, document support — as a set of **capability flags**. The
instrumentation loop and the policies read those flags; nothing in
Inkfoot branches on a provider's name.

## The matrix

| Provider | Integration | Prompt caching | Priced out of the box |
|---|---|---|---|
| Anthropic | auto-patched SDK shim | explicit markers (`cache_control`) | Claude family |
| OpenAI | auto-patched SDK shim | automatic prefix caching | gpt-4o, gpt-4o-mini, o1 |
| Gemini | auto-patched SDK shim | cache resources (`CachedContent`) | gemini-1.5-pro / -flash |
| Bedrock | provider class (no shim) | per model family | Claude on Bedrock |
| OpenAI-compat | instance-configured provider class | declared by you | everything, at $0 |

## Capability flags

Every provider answers `get_capabilities()` with a frozen
declaration:

| Flag | Meaning |
|---|---|
| `supports_tool_use` | The endpoint accepts tool/function schemas. |
| `supports_image_input` | Image blocks are accepted in requests. |
| `supports_document_block` | Native document (PDF) blocks are accepted. |
| `supports_prompt_cache` | Some form of prompt caching exists. |
| `prompt_cache_style` | `"explicit_marker"`, `"automatic"`, `"cache_resource"`, or `"none"`. |
| `cache_read_price_ratio` | Cache-read price as a fraction of fresh input. |
| `cache_write_price_ratio` | Cache-write price as a fraction of fresh input. |
| `supports_response_format_json` | A JSON response format can be requested. |
| `cheap_model_for_summariser` | The provider's cheap model, used by `CheapSummariser`. |

Policies act on these flags. `CacheControlPlacer`, for example,
advises explicit markers on Anthropic, creates a cache resource on
Gemini, and stays silent on a provider that declares
`prompt_cache_style="none"` — same policy object, no
provider-specific configuration.

The declared value of every flag for every shipped provider is
tabulated in the
[provider capability matrix](reference/provider-matrix.md).

## Anthropic and OpenAI

The original integrations: `inkfoot.instrument()` auto-detects the
installed SDK and patches its client methods. See
[Raw Provider SDK](frameworks/raw-sdk.md) for the walkthrough.

## Google Gemini

```bash
pip install "inkfoot[gemini]"
```

The shim patches `GenerativeModel.generate_content` and
`generate_content_async`. Model-bound state — the
`system_instruction` and `tools` you passed to the
`GenerativeModel` constructor — reaches the ledger even though it
never appears in the per-call arguments.

```python
import inkfoot
import google.generativeai as genai

inkfoot.instrument()

with inkfoot.agent_run(task="triage"):
    model = genai.GenerativeModel(
        "gemini-1.5-pro",
        system_instruction=BIG_SYSTEM_PROMPT,
    )
    model.generate_content("what failed in the deploy?")
```

Usage mapping notes:

- `usage_metadata.cached_content_token_count` lands in
  `cache_read_tokens`.
- `thoughts_token_count` (thinking models) folds into output and
  is broken out in `reasoning_tokens`.

### Cache resources

Gemini's caching is a server-side **resource**, not a request
marker: you create a `CachedContent` from the stable prefix, then
issue calls against it. With `CacheControlPlacer` active, Inkfoot
drives that flow for you:

1. When a run's stable prefix exceeds the provider's minimum
   cacheable size, the policy creates one `CachedContent` and
   emits a `cache_resource_created` event.
2. The creating call bills the prefix as a cache write
   (`cache_status="miss"`).
3. Subsequent calls are rebound to the resource and bill reads
   (`cache_status="hit"`).

Per-call `tools=` overrides skip the rebind (a cache-bound model
cannot take per-call tools), and a failed creation degrades to a
single `cache_control_advice` event — your calls proceed uncached.

## Amazon Bedrock

```bash
pip install "inkfoot[bedrock]"
```

There is no Bedrock shim: `boto3` generates its clients
dynamically from service definitions, so there is no stable
module-level method to patch. Instrumentation is at the provider
level — run `converse()` yourself and hand the response to
`BedrockProvider`:

```python
import boto3
from inkfoot.providers import BedrockProvider

provider = BedrockProvider("anthropic.claude-3-5-sonnet-20241022-v2:0")
client = boto3.client("bedrock-runtime", region_name="us-east-1")

response = client.converse(
    modelId=provider.DEFAULT_MODEL,
    messages=[{"role": "user", "content": [{"text": "…"}]}],
)
usage = provider.map_usage(response)
```

### Capabilities vary per model family

A Bedrock model id encodes its family, and the family decides the
capabilities:

| Model id prefix | Caching | Documents / images |
|---|---|---|
| `anthropic.` | explicit markers, Anthropic ratios | yes |
| `meta.llama` | none | no |
| `amazon.titan` | none | no |
| `mistral.` | none | no |
| `cohere.` | none | no |

Cross-region inference profiles (`us.anthropic.…`,
`eu.anthropic.…`) resolve through the geo prefix. An unknown
family resolves to the conservative no-cache shape rather than
raising, so a newly launched model never breaks instrumentation.

Usage mapping is uniform across families thanks to the Converse
API: `usage.inputTokens` / `outputTokens`, plus
`cacheReadInputTokens` / `cacheWriteInputTokens` on caching
models. As with Anthropic's native API, `inputTokens` excludes the
cached portion — Inkfoot adds it back so `input_tokens` is always
the full prompt size.

Only the Anthropic family is priced out of the box (AWS lists
Claude on Bedrock at parity with Anthropic direct). The other
families vary by region and purchasing model, so they report
tokens without a dollar estimate.

## OpenAI-compatible endpoints

One class covers every backend that speaks the OpenAI Chat
Completions protocol:

```python
import openai
from inkfoot.providers import OpenAICompatProvider, ProviderRegistry

provider = OpenAICompatProvider(
    base_url="http://localhost:11434/v1",  # Ollama
    model="llama3.2",
)
ProviderRegistry.register(provider)

client = openai.OpenAI(base_url=provider.base_url, api_key="ollama")
response = client.chat.completions.create(
    model=provider.model,
    messages=[{"role": "user", "content": "…"}],
)
usage = provider.map_usage(response)
```

Calls made through the OpenAI SDK are captured by the OpenAI shim
like any other OpenAI traffic; the `openai_compat` provider type
is how you *declare* the endpoint — its capabilities for policy
decisions, its usage mapping, and its pricing.

### Conservative defaults, operator overrides

The backends are heterogeneous, so the default declaration only
claims what every compat server supports: tool use yes; caching,
image input, document blocks, JSON response format no. If you know
your backend better, widen the declaration — pass a full
`Capabilities` instance, or a dict of field overrides applied on
top of the conservative base:

```python
provider = OpenAICompatProvider(
    base_url="https://api.together.xyz/v1",
    model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    api_key=TOGETHER_API_KEY,
    capabilities={"supports_response_format_json": True},
)
```

Incoherent overrides (a cache style without cache support, say)
fail at construction, not at policy time.

### Pricing

Self-hosted is free at the provider boundary: the pricing table
carries an `("openai_compat", "*")` wildcard row of zeros, so any
model under this provider type estimates at exactly $0 — present,
not unknown. Running against a paid compat endpoint? Add an exact
`("openai_compat", "<model>")` row; exact rows win over the
wildcard.

## Bring your own provider

Subclass `LLMProvider`, declare capabilities, map your usage
shape, and register:

```python
from inkfoot.providers import (
    Capabilities,
    LLMProvider,
    ProviderRegistry,
    TokenUsage,
)

class MyProvider(LLMProvider):
    PROVIDER_TYPE = "my-provider"
    DEFAULT_MODEL = "my-model-1"
    CAPABILITIES = Capabilities(...)

    def map_usage(self, response) -> TokenUsage:
        ...

ProviderRegistry.register(MyProvider())
```

Providers whose capabilities vary per model or per instance leave
`CAPABILITIES` unset and override `get_capabilities()` instead —
that is exactly how `BedrockProvider` and `OpenAICompatProvider`
work.

## Where to next

- [Raw Provider SDK](frameworks/raw-sdk.md) — the
  `instrument()` walkthrough for shim-patched SDKs.
- [Accuracy & Estimation](concepts/accuracy.md) — what the
  pricing rows mean and when they go stale.
- [Spot cache-miss patterns](recipes/spot-cache-misses.md) —
  reading cache statuses across a run.
