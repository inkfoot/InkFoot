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

### ::: inkfoot.tag

### ::: inkfoot.tag_node

### ::: inkfoot.tag_retrieval

### ::: inkfoot.checkpoint

### ::: inkfoot.report_cost

## Framework adapters

Per-framework adapters live under their own modules. Import the
namespace and call `.instrument(target)` once you've called
`inkfoot.instrument()` at startup.

### ::: inkfoot.langgraph.instrument

### ::: inkfoot.openai_agents.instrument

### ::: inkfoot.anthropic_agent.instrument

### ::: inkfoot.pydantic_ai.instrument

### ::: inkfoot.crewai.instrument

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
