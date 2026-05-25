# Inkfoot

> **Find the hidden cost trail in every AI agent run.**

Inkfoot is a causal token economics layer for LLM agents. It instruments
existing agent frameworks (LangGraph, OpenAI Agents SDK, Anthropic Agent
SDK, Pydantic AI, CrewAI) without requiring rewrites, attributes every
billed token to one of 13 causal categories, surfaces named cost smells
automatically, enforces declarative Token Contracts in runtime and CI,
and (in Cloud) replays past runs under different policies to prove
savings against real provider invoices.

**Status:** Phase 0 epics **E1** (Project + Storage Foundation),
**E2** (Causal Token Ledger), and **E3** (Pattern A Instrumentation)
have landed. The package now provides the package skeleton,
nanodollar money type, SQLite storage with WAL + two-tier write
semantics, aggregator worker, `inkfoot rebuild-aggregates` CLI, the
14-field Causal Token Ledger, per-provider Anthropic + OpenAI
translators with stable-prefix detection, the `tiktoken`-based
tokeniser layer with estimation flags, the pricing module
(`estimate_nanodollars` keyed by `(provider, model)`), and the
one-line wedge `inkfoot.instrument()` that monkey-patches Anthropic
+ OpenAI SDK calls with hook-isolation guarantees, replay-mode
content capture (ADR-0-9), and the three Phase 0 observation
policies (`BudgetCap`, `RetryThrottle`, `CacheControlPlacer`).
Subsequent epics (E4 smells, E5 report CLI, E6 rollout) build on
this foundation. The architecture spec + roadmap live in a separate
documentation repository (see project owner).

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
  _instrument.py                            # inkfoot.instrument() entry point (E3) — leading underscore so the submodule doesn't shadow the public callable on the package
  _run_context.py                           # ContextVar-based active-run pointer
  _shim_install.py                          # SDK auto-detect + install/uninstall
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
  policy/
    __init__.py                             # Policy ABC + IntegrationPattern + register_policies
    registry.py                             # PolicyRegistry singleton
    budget_cap.py                           # BudgetCap (observe-only in Phase 0)
    retry_throttle.py                       # RetryThrottle
    cache_control_placer.py                 # CacheControlPlacer (Anthropic)
  shims/
    _isolation.py                           # @isolated_hook / safely_run (ADR-0-3)
    _emit.py                                # shared event-emit pipeline
    anthropic.py                            # AnthropicShim (sync + async)
    openai.py                               # OpenAIShim (sync + async)
  storage/
    __init__.py                             # Storage Protocol (lazy SQLiteStorage)
    sqlite.py                               # SQLiteStorage + WAL pragmas + replay-mode write
    migrations.py                           # forward-only DDL (v1 = §5.5 + §5.5.1)
    aggregator.py                           # claim-and-project AggregatorWorker
  cli/
    main.py                                 # `inkfoot` entry point
    rebuild_aggregates.py                   # `inkfoot rebuild-aggregates`
tests/
  unit/                                     # 260 unit tests (E1 + E2 + E3)
  benchmarks/                               # `pytest-benchmark` hot-path budgets (storage + aggregator + shim)
.github/workflows/ci.yml                    # unit + benchmark on every PR
```

The architecture spec, roadmap, and per-phase epic docs live in a
separate documentation repository.

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
