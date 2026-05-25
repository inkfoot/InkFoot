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
  architecture-inkfoot.md                   # full technical design
  roadmap-inkfoot.md                        # phased delivery roadmap
  planned/                                  # phases not yet released
    README.md                               # phase index + capability matrix
    phase0/
      phase-0-classify.md                   # phase architecture
      inkfoot_phase0_development_epics.md   # epic + story breakdown
    phase1/
      phase-1-explain.md
      inkfoot_phase1_development_epics.md
    phase2/
      phase-2-enforce.md
      inkfoot_phase2_development_epics.md
    phase3/
      phase-3-prove.md
      inkfoot_phase3_development_epics.md
    phase4/
      phase-4-compound.md
      inkfoot_phase4_development_epics.md
    phase5/
      phase-5-enterprise.md
      inkfoot_phase5_development_epics.md
  released/                                 # phases that have shipped (empty)
```

When a phase ships, its `phaseN/` folder moves from `docs/planned/`
to `docs/released/`, preserving the architecture + epic docs as the
historical record.

## License

Apache 2.0 (target license per the roadmap; LICENSE file to be added
before Phase 1 public OSS launch).
