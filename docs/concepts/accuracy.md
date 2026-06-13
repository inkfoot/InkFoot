# Accuracy & Estimation

Inkfoot reports two kinds of numbers: those the provider gave us
exactly, and those we tokenised on our side and labelled as
estimates. The product is loud about which is which — every
estimated field carries a flag, every report knows the difference,
and a public validation harness lets you verify the math against
your own runs before you trust it.

This page covers:

- What's **exact** vs. what's **estimated** in the 14-field ledger.
- The `estimation_flags` mechanism: how Inkfoot tells you it
  isn't sure.
- The validation corpus + harness: how to verify the attribution
  for yourself.

## What's exact

These three numbers come straight from the provider's `usage`
block. They are billed exactly what they say:

| Ledger field | Source | Notes |
|---|---|---|
| `output_tokens` | `response.usage.output_tokens` (Anthropic) / `response.usage.completion_tokens` (OpenAI) | The generated tokens. Billed at the output rate. |
| `cache_creation_tokens` | `response.usage.cache_creation_input_tokens` (Anthropic) / cache hints (OpenAI) | Provider-reported. Billed at the cache-write rate. |
| `cache_read_tokens` | `response.usage.cache_read_input_tokens` (Anthropic) / cache hints (OpenAI) | Provider-reported. Billed at the cache-read rate. |

One exception: a **streamed** call only yields these numbers if
the stream carries a terminal usage payload. Most do (Anthropic's
`message_delta`, the Responses `response.completed` event, an
OpenAI Chat `stream_options={"include_usage": True}` chunk). When
one doesn't, `output_tokens` is tokeniser-estimated and the call
is flagged — see `stream_no_usage` / `stream_options_off` below.

A fourth provider-exact number sits underneath the ledger but is
visible in `inkfoot report`'s headline: the per-call total cost.
That number is computed from the per-category token splits times
the rate table in [`inkfoot.pricing`](../reference/api.md) —
exact tokens × pinned rates means the dollar figure is exact
relative to the pricing snapshot. (More on rate drift below.)

## What's estimated

The eleven *structural* input categories are tokeniser-derived.
Inkfoot pulls each piece of the request (system prompt, user
turn, tool schemas, prior turns, retrieved context, etc.) through
the appropriate tokeniser and assigns its token count to a
category. Two sources of error:

1. **Tokeniser drift.** Anthropic does not publish its tokeniser;
   Inkfoot falls back to `tiktoken`'s `o200k_base` encoder, which
   is within ~2% of Anthropic's actual count at typical context
   sizes but can diverge on heavy emoji / RTL / unusual code
   payloads.
2. **Structural ambiguity.** Some content — like a re-injected
   tool result that the agent loop reformats — could legitimately
   land in two categories. Inkfoot picks the most useful answer
   for cost attribution rather than the most defensible one and
   names the choice in the smell that fires.

The structural-input sum agrees with the provider's reported
`input_tokens` to within the
[`INPUT_INVARIANT_TOLERANCE`](../reference/api.md)
budget (2% by default). When it doesn't, the call is flagged —
see the next section.

## `estimation_flags`: how Inkfoot says "I'm not sure"

Every `NeutralCall` event carries an `estimation_flags` tuple of
short slugs naming the approximations applied to that call.
Common flags:

| Flag | Meaning |
|---|---|
| `tokeniser_fallback` | The Anthropic call was tokenised with `o200k_base` rather than the provider's own tokeniser. |
| `pricing_snapshot_stale` | The cost row used is older than the pinned freshness window (default 90 days). |
| `attribution_below_tolerance` | The structural input sum diverged from `usage.input_tokens` by more than the tolerance. |
| `cache_inferred` | The provider didn't return a cache breakdown; Inkfoot inferred one from a stable-prefix detection. |
| `responses_shape_unknown:<key>` | An OpenAI Responses API reply carried a top-level key the translator doesn't map yet. The call still translated; the named key was ignored. |
| `stream_no_usage` | A streamed call ended without a terminal usage payload, so `output_tokens` was tokeniser-estimated from the streamed text. |
| `stream_options_off` | An OpenAI Chat streamed call omitted `stream_options={"include_usage": True}`, so no usage was streamed; `output_tokens` was tokeniser-estimated. Pass that option for an exact count. |
| `output_tokens` | The output count is an estimate, not provider-reported. Accompanies the two stream flags above. |

`inkfoot report` surfaces estimation flags in the run footer when
any flag is set on any call in the run. `inkfoot benchmark`
includes them per-scenario so a CI diff can fail loudly when a
new flag appears.

The same data is exported on the
[`inkfoot.estimation_flags` OTel attribute](otel.md#mapping-table),
so downstream telemetry pipelines never lose the provenance
either.

!!! info "What flags are *not*"

    Flags don't say "this number is wrong" — they say "this
    number was approximated." The validation harness below is the
    way to decide whether the approximation is good enough for
    your workload.

## Validation harness

Inkfoot ships a corpus + harness so you can verify the attribution
maths against runs that look like yours. Two pieces:

- A **validation corpus** under `tests/fixtures/validation/`
  containing real provider responses (anonymised) paired with a
  `labels.json` ground-truth file naming the expected category
  for each fragment of the request.
- The **harness script** at `scripts/validate_attribution.py`
  which loads the corpus, runs each fixture through the matching
  translator, and asserts the per-category numbers match the
  labels to within the documented tolerance.

The harness is the source of truth for two CI gates:

1. **Within-tolerance attribution.** Every fixture's structural
   sum must agree with the recorded `usage.input_tokens` value.
2. **Smell determinism.** Every fixture's recorded smell hits
   must match what the engine produces today.

### Run it locally

```bash
pip install -e ".[dev]"
python scripts/validate_attribution.py
```

You'll see one line per fixture, plus a summary tabulating any
divergence. Non-zero exit means a gate failed.

### Add your own fixtures

1. Capture a real provider response. The replay-mode capture path
   in [Tracking Runs](tracking-runs.md) writes the request /
   response pair into the `event_contents` sibling table; export
   them with the
   [Extract replay fixtures](../recipes/extract-fixtures.md)
   recipe.
2. Add the JSON pair to `tests/fixtures/validation/<your-name>/`
   and edit the `labels.json` to name the expected category for
   each piece of the request.
3. Re-run the harness; it picks up the new directory
   automatically.

Adding your own fixtures is the recommended way to qualify
Inkfoot for a new domain — agent telemetry that talks heavily
about code looks very different from agent telemetry that talks
about email.

## Pricing freshness

Inkfoot's cost math is *exact relative to the pinned pricing
snapshot*. The snapshot lives in
[`inkfoot.pricing.PRICING_ND_PER_TOKEN`](../reference/api.md).
Each row has a `last_verified` date; the report rendering layer
adds a `pricing_snapshot_stale` flag to any call whose model's
row is older than 90 days.

When the pricing changes upstream, the snapshot bumps in a
single coordinated PR — pricing rows always land together so an
intermediate state never claims "Sonnet got cheaper but Haiku
didn't yet."

Two kinds of rows deviate from the exact `(provider, model)`
shape:

- **Wildcard rows.** `("openai_compat", "*")` prices every model
  under the OpenAI-compatible provider type at exactly $0 —
  self-hosted is free at the provider boundary, and an explicit
  zero is more honest than "unknown". An exact
  `(provider, model)` row always wins over the wildcard, which is
  how a paid compat endpoint gets real prices. See
  [Providers](../providers.md).
- **Unpriced models.** A model with no row (and no wildcard)
  estimates as `None` — tokens are still attributed, dollars are
  simply not claimed. Bedrock's non-Anthropic families ship this
  way because their list prices vary by region and purchasing
  model.

## What this isn't

<div class="not-this" markdown>
**This isn't a billing reconciliation.** Inkfoot's per-call cost
is a faithful application of the pricing snapshot to the
captured tokens. It is *not* a substitute for the provider's
invoice — promotional credits, custom rate cards, free tiers,
and provider-side metering corrections all sit between the
captured tokens and the eventual bill. The number Inkfoot shows
you is "what the publicly-listed rates would have charged for
these tokens" and that's a useful answer for the questions
Inkfoot is built to answer, but it isn't a financial primary
source.
</div>
