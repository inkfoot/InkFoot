# Python API

The public Python surface of Inkfoot — what
`from inkfoot import ...` exposes — is enumerated here. Anything
not on this page is implementation detail and may move between
releases.

The docstrings are pulled directly from the source via
[mkdocstrings](https://mkdocstrings.github.io/), so this page
stays in lockstep with the runtime contract.

## Top-level functions

### ::: inkfoot.instrument

### ::: inkfoot.agent_run

### ::: inkfoot.set_outcome

### ::: inkfoot.outcomes.set_outcome_from_heuristic

### ::: inkfoot.tag

### ::: inkfoot.tag_node

### ::: inkfoot.tag_retrieval

### ::: inkfoot.checkpoint

### ::: inkfoot.report_cost

## Framework adapters

Per-framework adapters live under their own modules. Import the
namespace and call `.instrument(target)` once you've called
`inkfoot.instrument()` at startup. (The LangChain callback
handler needs no per-target call — `inkfoot.instrument()`
registers it globally when `langchain-core` is importable.)

### ::: inkfoot.langchain.instrument

### ::: inkfoot.langchain.InkfootCallbackHandler

### ::: inkfoot.langgraph.instrument

### ::: inkfoot.openai_agents.instrument

### ::: inkfoot.anthropic_agent.instrument

### ::: inkfoot.pydantic_ai.instrument

### ::: inkfoot.crewai.instrument

## Providers

The provider abstraction under `inkfoot.providers` — capability
declarations, usage mapping, and the registry. The
Anthropic, OpenAI, and Gemini providers are auto-seeded into the
registry; you instantiate a provider yourself for Bedrock, for
OpenAI-compatible endpoints, or when bringing your own. See
[Providers](../providers.md) for the capability matrix and
per-provider walkthroughs.

### ::: inkfoot.providers.LLMProvider

### ::: inkfoot.providers.Capabilities

### ::: inkfoot.providers.TokenUsage

### ::: inkfoot.providers.ProviderRegistry

### ::: inkfoot.providers.BedrockProvider

### ::: inkfoot.providers.OpenAICompatProvider

## Observation policies

Policy classes you pass into
`inkfoot.instrument(policies=[...])`.

### ::: inkfoot.policy.BudgetCap

### ::: inkfoot.policy.RetryThrottle

### ::: inkfoot.policy.CacheControlPlacer

## Modification policies

Request-rewriting policies. These require an active framework
adapter — register them via
`inkfoot.policy.register_policies([...])` after the adapter's
`instrument()` call. See
[Modification Policies](../concepts/modification-policies.md).

### ::: inkfoot.policy.LazyToolExposure

### ::: inkfoot.policy.CheapSummariser

### ::: inkfoot.policy.register_policies

## Storage backends

The default SQLite backend needs no configuration. For
multi-process deployments writing to a shared server, import
`PostgresStorage` from `inkfoot.storage` and pass an instance to
`inkfoot.instrument(storage=...)` — see the
[Postgres Backend](../concepts/postgres.md) concept page for the
operational picture (aggregation worker, migration, environment
variables).

### ::: inkfoot.storage.postgres.PostgresStorage

## Redaction

When `inkfoot.instrument(capture_mode="replay")` persists request and
response bodies, a regex floor masks the secret shapes that must never
reach disk. Implement `RedactionHook` and pass it as
`inkfoot.instrument(redaction_hook=...)` to mask
organisation-specific shapes on top of the floor — both run. See
[Services & multi-replica deployments](../operations/services-and-multi-replica.md#redaction-is-required-before-replay-capture-in-services)
for the operational picture.

### ::: inkfoot.storage.redaction.RedactionHook

### ::: inkfoot.storage.redaction.RedactionContext

## Exceptions

### ::: inkfoot.errors.InkfootError

### ::: inkfoot.errors.PolicyNotSupported

### ::: inkfoot.errors.StorageError

## Internal data shapes (informational)

The dataclasses + enums below aren't part of the SemVer contract,
but you'll encounter them when extracting data manually from
storage — they appear in JSON payloads, in OpenTelemetry attrs,
and in test fixtures.

### ::: inkfoot.normalise.NeutralCall

### ::: inkfoot.ledger.CausalTokenLedger

### ::: inkfoot.pricing.estimate_nanodollars

The full pricing table (`PRICING_ND_PER_TOKEN`) lives in the
same module; see the [Accuracy & Estimation](../concepts/accuracy.md)
concept page for what the rows mean and when they go stale.
