# Changelog

All notable changes to Inkfoot are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-14

First public release. The headline: **Inkfoot is a drop-in causal-cost
layer for the LangChain-shaped majority of LLM apps.** One handler
captures usage across every LangChain chat-model integration, and the
raw-SDK shims cover the providers people call directly — with the two
layers de-duplicated so a call is never counted twice.

Earlier development happened in pre-release builds that were not part
of this public launch; `1.0.0` is the first version offered as a
stable, supported release.

### Added

**LangChain, drop-in**

- `inkfoot.langchain.InkfootCallbackHandler` and
  `inkfoot.langchain.instrument()` capture usage across ChatAnthropic,
  ChatOpenAI (Chat Completions *and* Responses), AzureChatOpenAI,
  ChatGoogleGenerativeAI, and ChatBedrock by reading LangChain's
  normalised `usage_metadata`.
- `inkfoot.instrument(langchain="auto")` registers the handler
  automatically whenever `langchain-core` is importable.
- Cross-layer de-duplication: when the callback handler and a raw-SDK
  shim both observe the same call, exactly one event is recorded
  (keyed on the provider response id), keeping the richer payload.

**Providers and APIs**

- Monkey-patch shims for the Anthropic and OpenAI SDKs (Chat
  Completions and the Responses API) and Gemini, installed by a single
  `inkfoot.instrument()` call with SDK auto-detection.
- OpenAI Responses API translator mapping the renamed request/response
  shapes onto the same ledger, including reasoning and cached tokens;
  unknown fields degrade gracefully instead of crashing.
- `AnthropicBedrock` calls are captured and tagged distinctly, with
  Bedrock-rate pricing rows.

**Streaming**

- Streaming capture across Anthropic, OpenAI Chat Completions, and the
  OpenAI Responses API. A property-tested invariant guarantees wrapped
  streams yield byte-identical chunks to the caller.
- Graceful fallback to tokeniser estimation when a stream closes
  without a usage block, flagged in the recorded event.

**Embeddings**

- Opt-in embedding capture (`inkfoot.instrument(embeddings=True)`)
  recorded as a separate `embedding_call` event kind, accounted apart
  from the causal token ledger and surfaced in its own report section.

**Framework adapters**

- Adapters for LangGraph (per-node attribution; validated against the
  0.3.x and 1.0.x lines), the OpenAI Agents SDK, the Anthropic Agent
  SDK, Pydantic AI, and CrewAI.

**Contracts and policies**

- Declarative Token Contracts authored in YAML, enforced both at
  runtime (a per-call degrade ladder: warn, switch to a cheaper model,
  or block) and in CI.
- Observation policies (`BudgetCap`, `RetryThrottle`,
  `CacheControlPlacer`) and modification policies (`LazyToolExposure`,
  `CheapSummariser`) with an A/B mode and a kill switch.

**Cost smells**

- A rule-based smell engine with a catalogue of named cost smells and
  per-run remediation hints.
- Cost-per-success reporting and tag-based group-by for aggregate
  views.
- The community **Cost Smell Library**: a versioned smell schema, a
  bundled offline snapshot shipped inside the wheel, and a browsable
  catalogue site, open for contribution.

**Storage and observability**

- Local SQLite storage by default (WAL, two-tier writes, background
  aggregator) and an optional Postgres backend
  (`inkfoot[postgres]`) with advisory-lock aggregation and a resumable
  `inkfoot migrate --to postgres` cutover.
- A redaction hook with a default regex floor (emails, common API-key
  shapes, JWTs) that runs at the storage boundary before any content
  is written in replay mode.
- Bidirectional OpenTelemetry support: export every `llm_call` as a
  GenAI span, or ingest GenAI spans from an OTLP/JSON endpoint.

**CLI and CI**

- The `inkfoot` CLI: `report`, `tag`, `benchmark`, `diff`,
  `rebuild-aggregates`, `aggregator-worker`, `migrate`, and `contract`.
- The composite `inkfoot/diff-action` GitHub Action wraps
  `benchmark` + `diff` behind a one-line workflow step with a sticky PR
  comment and `ok`/`warn`/`fail` verdicts.

**Privacy**

- An opt-in, anonymous install ping — off by default, asked once at an
  interactive terminal, and fully documented in the privacy guide. It
  honours `DO_NOT_TRACK` and never transmits any of your data.

### Packaging

- Supports Python 3.10, 3.11, 3.12, and 3.13.
- Optional extras: `langchain`, `langgraph`, `openai-agents`,
  `anthropic-agent`, `pydantic-ai`, `crewai`, `gemini`, `bedrock`,
  `postgres`, `docs`, `lint`, and `all`.
- Published to PyPI via Trusted Publishing (OIDC), with a post-release
  smoke install on every supported interpreter.

[1.0.0]: https://github.com/inkfoot/inkfoot/releases/tag/v1.0.0
