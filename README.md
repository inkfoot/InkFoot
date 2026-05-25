# Inkfoot

> **Find the hidden cost trail in every AI agent run.**

Inkfoot is a causal token economics layer for LLM agents. It instruments
existing agent frameworks (LangGraph, OpenAI Agents SDK, Anthropic Agent
SDK, Pydantic AI, CrewAI) without requiring rewrites, attributes every
billed token to one of 13 causal categories, surfaces named cost smells
automatically, enforces declarative Token Contracts in runtime and CI,
and (in Cloud) replays past runs under different policies to prove
savings against real provider invoices.

**Status:** Phase 0 epics **E1** (Project + Storage Foundation) and
**E2** (Causal Token Ledger) have landed. The package now provides
the package skeleton, nanodollar money type, SQLite storage with WAL
+ two-tier write semantics, aggregator worker, `inkfoot
rebuild-aggregates` CLI, plus the 14-field Causal Token Ledger,
per-provider Anthropic + OpenAI translators with stable-prefix
detection, the `tiktoken`-based tokeniser layer with estimation
flags, and the pricing module (`estimate_nanodollars` keyed by
``(provider, model)``). Subsequent epics (E3 Pattern A shims, E4
smells, E5 report CLI, E6 rollout) build on this foundation. See
`docs/roadmap-inkfoot.md` for the phased delivery plan and
`docs/architecture-inkfoot.md` for the technical design.

## Quickstart (development)

Requires Python 3.10+.

```bash
git clone https://github.com/anirbanbhat/InkFoot.git
cd InkFoot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Sanity check
python -c "import inkfoot; print(inkfoot.__version__)"

# Run unit tests
pytest tests/unit -v

# Run the storage hot-path benchmark (p95 < 1 ms gate)
pytest tests/benchmarks --benchmark-only

# Recover projected totals on ~/.inkfoot/runs.db after a crash
inkfoot rebuild-aggregates
```

CI (`.github/workflows/ci.yml`) runs unit tests on Python 3.10 / 3.11
/ 3.12 and uploads the storage benchmark JSON as an artefact per PR.

## Repository layout

```
inkfoot/                                    # the Python package
  __init__.py                               # public re-exports (frozen surface)
  _version.py                               # SemVer skeleton
  errors.py                                 # InkfootError, PolicyNotSupported, ...
  money.py                                  # Nanodollar type (ADR-0-4)
  ledger.py                                 # 14-field CausalTokenLedger + invariant
  pricing.py                                # PRICING_ND_PER_TOKEN + estimate_nanodollars
  run.py                                    # Run + InMemoryRunState dataclasses
  tokenisers.py                             # tiktoken + Anthropic fallback
  normalise/
    __init__.py                             # NeutralCall + stable-prefix detector
    anthropic.py                            # AnthropicTranslator
    openai.py                               # OpenAITranslator
  storage/
    __init__.py                             # Storage Protocol (lazy SQLiteStorage)
    sqlite.py                               # SQLiteStorage + WAL pragmas
    migrations.py                           # forward-only DDL (v1 = §5.5 + §5.5.1)
    aggregator.py                           # claim-and-project AggregatorWorker
  cli/
    main.py                                 # `inkfoot` entry point
    rebuild_aggregates.py                   # `inkfoot rebuild-aggregates`
tests/
  unit/                                     # 189 unit tests (E1 + E2)
  benchmarks/                               # `pytest-benchmark` hot-path budgets
.github/workflows/ci.yml                    # unit + benchmark on every PR
docs/
  architecture-inkfoot.md                   # full technical design
  roadmap-inkfoot.md                        # phased delivery roadmap
  planned/                                  # phases not yet released
    README.md                               # phase index + capability matrix
    phase0/
      phase-0-classify.md                   # phase architecture
      inkfoot_phase0_development_epics.md   # epic + story breakdown
    phase1/ ... phase5/                     # (same shape per phase)
  released/                                 # phases that have shipped (empty)
```

When a phase ships, its `phaseN/` folder moves from `docs/planned/`
to `docs/released/`, preserving the architecture + epic docs as the
historical record.

## Operator notes

- **PyPI name reservation:** `inkfoot` to be reserved on PyPI before
  the Phase 1 public OSS launch (E1-S1 T4). Until then the package
  is installable only from source.
- **Domain:** `inkfoot.dev` is reserved for the docs site that ships
  with Phase 1 (EX10).
- **Default DB path:** `~/.inkfoot/runs.db`. Override via the
  `INKFOOT_HOME` environment variable (parent dir) or pass an
  explicit `path=` to `SQLiteStorage`.
- **Aggregator poll interval:** defaults to 500 ms; override with
  `INKFOOT_AGGREGATOR_INTERVAL_MS=<int>`. Values under 10 ms are
  clamped to the 10 ms floor with a warning.

## License

Apache 2.0 — see [LICENSE](LICENSE).
