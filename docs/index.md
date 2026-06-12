---
hide:
  - navigation
  - toc
---

# Inkfoot

<p class="lede">
  Find the hidden cost trail in every AI agent run. Inkfoot
  instruments your existing agents, attributes every billed token
  to one of fourteen causal categories, and surfaces named cost
  smells the moment they fire — so the prompt change that doubled
  your bill shows up <em>before</em> the invoice does.
</p>

```bash
pip install inkfoot
```

<div class="cta-row" markdown>
[:material-rocket-launch-outline: Get to your first report in 5 minutes](quickstart.md){ .md-button .md-button--primary }
[:material-school-outline: What's the Causal Token Ledger?](concepts/causal-token-ledger.md){ .md-button }
[:material-github: Source on GitHub](https://github.com/inkfoot/InkFoot){ .md-button }
</div>

## What Inkfoot does

Inkfoot is a **causal token economics layer for LLM agents**. You
add `inkfoot.instrument()` at startup, optionally wrap your
agent's entry-point with `@inkfoot.agent_run(task=...)`, and
Inkfoot does four things:

| | |
|---|---|
| :material-chart-areaspline: **Per-token attribution** | Every billed token lands in one of 14 categories (`system_static`, `tool_result`, `retrieved_context`, `cache_read`, …) so you can see *why* a call cost what it cost. |
| :material-alert-circle-outline: **Cost smells** | Named patterns like `unstable-prompt-prefix` and `oversized-tool-result-recycled` fire automatically with a one-line remediation. |
| :material-source-branch: **CI cost review** | `inkfoot benchmark` + `inkfoot diff` + the published GitHub Action turn a PR-time regression into a comment on the pull request. |
| :material-export: **OpenTelemetry, both ways** | Ingest existing `gen_ai.*` spans from any OTel collector and re-emit Inkfoot events to Honeycomb / Grafana / Datadog. |

## Read next

<div class="grid cards" markdown>

-   :material-rocket-launch-outline: __[Quickstart](quickstart.md)__

    `pip install` → 5 lines of code → first report. Pinned at
    under 5 minutes for a first-time visitor.

-   :material-school-outline: __[Concepts](concepts/causal-token-ledger.md)__

    The 14-field ledger, the smell catalogue, and what's exact
    vs. estimated.

-   :material-tools: __[Recipes](recipes/find-expensive-agent.md)__

    Task-oriented walkthroughs: find your most expensive agent,
    spot cache-miss patterns, set up CI cost review.

-   :material-application-cog-outline: __[Framework guides](frameworks/langchain.md)__

    LangChain, LangGraph, OpenAI Agents SDK, Anthropic Agent
    SDK, and the raw provider SDKs.

-   :material-cloud-outline: __[Providers](providers.md)__

    The capability matrix: Anthropic, OpenAI, Gemini, Bedrock,
    and OpenAI-compatible endpoints (vLLM, Ollama, Together, …).

</div>

## Status

Inkfoot is open-source under Apache 2.0. The project is currently
pre-1.0 — the SemVer contract is the public surface enumerated on
the [Python API reference](reference/api.md); everything else is
implementation detail and may move between releases.

Found a bug or have an idea? [Open an issue](https://github.com/inkfoot/InkFoot/issues)
or jump straight to a [pull request](https://github.com/inkfoot/InkFoot/pulls).
