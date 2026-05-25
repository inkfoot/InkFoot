# Inkfoot

> **Find the hidden cost trail in every AI agent run.**

Inkfoot is a causal token economics layer for LLM agents. It instruments
existing agent frameworks (LangGraph, OpenAI Agents SDK, Anthropic Agent
SDK, Pydantic AI, CrewAI) without requiring rewrites, attributes every
billed token to one of 13 causal categories, surfaces named cost smells
automatically, enforces declarative Token Contracts in runtime and CI,
and (in Cloud) replays past runs under different policies to prove
savings against real provider invoices.

**Status:** approved design; not yet implemented. See
`docs/roadmap-inkfoot.md` for the phased delivery plan and
`docs/architecture-inkfoot.md` for the technical design.

## Repository layout

```
docs/
  architecture-inkfoot.md           # full technical design
  roadmap-inkfoot.md                # phased delivery roadmap
  phases/                           # per-phase implementation specs
    README.md                       # index of phase docs
    phase-0-classify.md
    phase-1-explain.md
    phase-2-enforce.md
    phase-3-prove.md
    phase-4-compound.md
    phase-5-enterprise.md
```

Epics under each phase will be drafted as separate documents once each
phase is approved for execution.

## License

Apache 2.0 (target license per the roadmap; LICENSE file to be added
before Phase 1 public OSS launch).
