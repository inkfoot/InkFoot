# The Causal Token Ledger

Every report Inkfoot produces is sliced along the same fourteen
fields: eleven structural causes that name *why* a token was
included in the request, two cache overlays that name *how the
provider billed it*, and one output total. The ledger is the
load-bearing primitive — once you know which field swelled, the
fix usually picks itself.

This page covers what each field measures, where the
attribution comes from, and how to read a real report. For
"what's exact vs. estimated", see [Accuracy & Estimation](accuracy.md).

## The fourteen fields at a glance

| # | Field | What it measures |
|---|---|---|
| 1 | `system_static_tokens` | The stable, unchanging prefix of your system prompt across this run. |
| 2 | `system_dynamic_tokens` | The drifting tail of your system prompt — content that changed call-over-call. |
| 3 | `user_input_tokens` | The most recent user turn — "what the user just asked". |
| 4 | `tool_schema_tokens` | The serialised tool definitions you sent in the `tools` array. |
| 5 | `tool_result_tokens` | All tool-result blocks across the messages array — every tool's output. |
| 6 | `retrieved_context_tokens` | Text you marked with `inkfoot.tag_retrieval(...)` since the last call. |
| 7 | `memory_tokens` | Prior turns in the messages array (everything that isn't the current user turn or a tool result). |
| 8 | `retry_overhead_tokens` | Tokens spent on retries. Populated when retry classification is enabled. |
| 9 | `summariser_tokens` | Tokens consumed by an in-line summariser policy. |
| 10 | `reasoning_tokens` | Provider-reported thinking-block tokens on reasoning models (`thinking` content blocks on Anthropic, `reasoning_tokens` on OpenAI o-series). |
| 11 | `guardrail_tokens` | Tokens spent on guardrail checks. |
| 12 | `cache_creation_tokens` | Provider-reported tokens *written* into the prompt cache by this call. |
| 13 | `cache_read_tokens` | Provider-reported tokens *served from* the prompt cache by this call. |
| 14 | `output_tokens` | Provider-reported generated output tokens. |

The first eleven are *structural* — Inkfoot tokenises pieces of your
request and assigns them a category. Their sum approximates the
provider's "total billed input" within a few percent.

`cache_creation_tokens` and `cache_read_tokens` are *billing overlays*
lifted straight from the provider's `usage` response. They aren't added
on top of the structural categories — they report how the provider
*billed* the input tokens that were already counted structurally.

`output_tokens` is exact: read directly from the response and billed
separately at the output rate.

## How to read a report

When `inkfoot report --run <id>` prints something like:

```
Causal attribution:
  system_static       42.1%  ██████░░░░░░  $0.0052
  tool_result         28.4%  ███░░░░░░░░░  $0.0035  ⚠ oversized
  user_input          14.7%  █░░░░░░░░░░░  $0.0018
  output              10.3%  █░░░░░░░░░░░  $0.0013
  cache_read           4.5%  ░░░░░░░░░░░░  $0.0005
```

…you're looking at the share of dollar cost contributed by each
category for this run.

A `⚠` marker beside a row means a smell has tied its diagnosis to that
specific category — for example, the `oversized-tool-result-recycled`
smell anchors to `tool_result_tokens`. See [Cost Smells](cost-smells.md)
for the full list.

Zero-value rows are hidden by default. Pass `--show-zero` to render all
fourteen.

## Per-provider mapping

The structural attribution uses your provider's tokeniser:

- **OpenAI:** `tiktoken.encoding_for_model(model)` — exact counts.
- **Anthropic:** the official `anthropic.tokenize` when available;
  otherwise a `tiktoken` fallback with the `o200k_base` encoding (~2-5%
  drift on English prose).

Whenever the fallback is used, that field is flagged as estimated in
`NeutralCall.estimation_flags`. Reports respect the flag — fields with
estimated counts are marked so you know which dollar figures are
approximate.

### Anthropic-specific behaviour

- `output_tokens` ← `response.usage.output_tokens`.
- `cache_read_tokens` ← `response.usage.cache_read_input_tokens`.
- `cache_creation_tokens` ← `response.usage.cache_creation_input_tokens`.
- `reasoning_tokens` ← `usage.thinking_tokens` if present, otherwise
  the sum of tokens reported on `thinking` content blocks in the
  response.
- `system_static_tokens` / `system_dynamic_tokens` come from the
  *stable-prefix detector*: across the run, Inkfoot tracks the longest
  character-level common prefix of every system block it has seen. Each
  call's system block is split into the matching prefix (static) and
  the remainder (dynamic). The prefix can only shorten — never grow —
  across the run.

### OpenAI-specific behaviour

- `output_tokens` ← `usage.completion_tokens`.
- `cache_read_tokens` ← `usage.prompt_tokens_details.cached_tokens`.
- `cache_creation_tokens` is **always zero** — OpenAI does not bill for
  cache writes.
- `reasoning_tokens` ← `usage.completion_tokens_details.reasoning_tokens`
  on o-series models.

## Cost estimation

Each field's tokens are priced against the bundled pricing snapshot:

| Field | Rate |
|---|---|
| Structural categories (11) | `row.input` (fresh-input rate) |
| `cache_read_tokens` | `row.cache_read` |
| `cache_creation_tokens` | `row.cache_write` (zero for OpenAI) |
| `output_tokens` | `row.output` |

The headline dollar figure at the top of the report uses the
"fresh-input minus cache" math (so the cached portion isn't double-counted),
while the bar-chart bars show what each category *would* cost at the
full input rate. The difference becomes visible when a run has
significant cache hits.

For unknown `(provider, model)` pairs, Inkfoot still prints token
counts but omits the dollar figures.

## Validation invariant

For every call, Inkfoot checks:

- `ledger.input_total` (sum of the eleven structural fields) is within
  2% of the provider's reported total billed input.
- `ledger.output_total` exactly equals the provider's reported
  `output_tokens`.

If validation fails, the discrepancy shows up as an assertion in
internal validation runs — it doesn't affect your application's
runtime behaviour.

## Why these fields?

Each category names a *fixable* cause of cost.

- `system_dynamic_tokens` going up means your system prompt isn't
  cacheable.
- `tool_result_tokens` dominating means your tools return more than the
  model needs.
- `memory_tokens` growing call-over-call means your conversation history
  is unbounded.
- `cache_creation_tokens` exceeding `cache_read_tokens` means you're
  paying to write the cache but not reading from it enough to amortise.

Once you can see which category dominates, the next step is to fix it —
which is what the smell engine helps with. See
[Cost Smells](cost-smells.md).
