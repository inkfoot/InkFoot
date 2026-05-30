# Inkfoot

> **Find the hidden cost trail in every AI agent run.**

Inkfoot is a causal token economics layer for LLM agents. It instruments
existing agent frameworks (LangGraph, OpenAI Agents SDK, Anthropic Agent
SDK, Pydantic AI, CrewAI) without requiring rewrites, attributes every
billed token to one of 13 causal categories, surfaces named cost smells
automatically, enforces declarative Token Contracts in runtime and CI,
and (in Cloud) replays past runs under different policies to prove
savings against real provider invoices.

**Status:** Phase 0 is **code-complete** and Phase 1 E1 + E2 + E3
are now landed. Phase 0 epics E1–E6 shipped the Pattern-A SDK
shims, the 14-field Causal Token Ledger, the smell engine, the
report CLI, the validation harness, and the perf gates. Phase 1
E1 ships Pattern-C framework adapters (LangGraph, OpenAI Agents
SDK, Anthropic Agent SDK) + the Pattern-B ergonomic helpers
(`tag_node`, `checkpoint`) — `inkfoot.langgraph.instrument(graph)`
gives per-node attribution via `inkfoot report --run <id>
--group-by node`. Phase 1 E2 ships the CI cost-review workflow:
`inkfoot benchmark` runs scenario suites and emits a stable JSON
artefact; `inkfoot diff` compares two artefacts and produces a
Markdown PR comment + JSON report with an `ok|warn|fail` verdict
and exit codes; the composite `inkfoot/diff-action` GitHub Action
wraps both behind a one-line workflow step with a sticky PR
comment. Phase 1 E3 ships bidirectional OpenTelemetry GenAI
compatibility: `inkfoot.instrument(otel_ingest_port=4318)` opens
a stdlib-only OTLP/JSON receiver that translates `gen_ai.*`
spans into Inkfoot's 14-field ledger (deduplicated against the
native shim via ADR-1-2), and
`inkfoot.instrument(otel_export_endpoint=...)` mirrors every
`llm_call` event back out as an OTel span (smells / outcomes as
logs) to any collector. The remaining Phase 0 work is the
*operator-process* half of E6 (six weeks of production
exposure, 50 labelled runs, weekly smell review, go/no-go
decision). Phase 1 E4–E6 (smell rendering + tail, docs site, OSS
launch) are still ahead. The architecture spec + roadmap +
per-phase epic docs live in a separate documentation repository
(see project owner).

The user-facing surface today: `inkfoot.instrument()` to monkey-
patch the SDKs, `@inkfoot.agent_run(task=...)` decorator + context
manager for run scoping, `inkfoot.set_outcome / tag / tag_retrieval
/ tag_node / checkpoint / report_cost` for in-run metadata,
`inkfoot.langgraph.instrument(graph)` /
`inkfoot.openai_agents.instrument(agent)` /
`inkfoot.anthropic_agent.instrument(agent)` for framework
adapters (Pattern C — per-node attribution + tool-dispatch events),
the rule-based smell engine with five built-in cost smells, and
the `inkfoot` CLI with `report` (single-run attribution bar chart +
smells, or aggregate `--last 7d --group-by task` with runs / avg_$
/ p95_$ / success% / cost-per-success, or single-run
`--group-by node` for per-LangGraph-node ledger totals), `tag`
(late tagging), `rebuild-aggregates`, `benchmark` (scenario
runner emitting the Phase 1 JSON artefact), and `diff`
(structured comparison between two artefacts with `ok/warn/fail`
verdicts and `0/1/2` exit codes for CI).
Under it: nanodollar money type, SQLite storage with WAL + two-tier
writes, claim-and-project aggregator, the 14-field Causal Token
Ledger, per-provider Anthropic + OpenAI translators with
stable-prefix detection, `tiktoken`-based tokenisers with
estimation flags, the pricing module, and the three Phase 0
observation policies (`BudgetCap`, `RetryThrottle`,
`CacheControlPlacer`). Six §9.1 perf gates run on every PR.

## Quickstart (development)

Requires Python 3.10+.

```bash
git clone https://github.com/inkfoot/InkFoot.git
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
  adapters/                                 # Phase 1 / E1: framework adapters (Pattern C)
    __init__.py                             # FrameworkAdapter Protocol re-export
    base.py                                 # FrameworkAdapter + Instrumentation Protocols
    _registry.py                            # AdapterRegistry singleton + get_active_adapter
    langgraph.py                            # LangGraph adapter (per-node attribution + tools fingerprint)
    openai_agents.py                        # OpenAI Agents SDK adapter (tool-dispatched events)
    anthropic_agent.py                      # Anthropic Agent SDK adapter
  langgraph.py                              # Top-level convenience: inkfoot.langgraph.instrument(graph)
  openai_agents.py                          # Top-level convenience: inkfoot.openai_agents.instrument(agent)
  anthropic_agent.py                        # Top-level convenience: inkfoot.anthropic_agent.instrument(agent)
  smells/
    __init__.py                             # CostSmell + DetectionResult + DEFAULT_SMELLS + registry
    engine.py                               # SmellEngine (lazy, off the hot path)
    _helpers.py                             # event-payload parsing + pricing lookups
    unstable_prompt_prefix.py               # 5 Phase 0 smells, one per file
    runaway_retry_loop.py
    oversized_tool_result_recycled.py
    expensive_model_low_entropy.py
    recurring_cache_writes.py
  storage/
    __init__.py                             # Storage Protocol (lazy SQLiteStorage)
    sqlite.py                               # SQLiteStorage + WAL pragmas + replay-mode write
    migrations.py                           # forward-only DDL (v1 = §5.5 + §5.5.1)
    aggregator.py                           # claim-and-project AggregatorWorker
  _run_lifecycle.py                         # E5: @agent_run + set_outcome/tag/tag_retrieval/report_cost
  cli/
    main.py                                 # `inkfoot` entry point (report / rebuild-aggregates / tag)
    rebuild_aggregates.py                   # `inkfoot rebuild-aggregates`
    report.py                               # `inkfoot report` — bar chart + smells (renderer is pure)
    tag.py                                  # `inkfoot tag <run-id> <key> <value>` — late tagging
scripts/
  validate_attribution.py                   # E6: validation harness — fails CI when per-category mean error > 10%
  extract_run_fixtures.py                   # E6: nightly extractor for the validation corpus
tests/
  unit/                                     # 461 unit tests (Phase 0 E1–E6 + Phase 1 E1)
  integration/                              # Phase 1 E1: per-framework e2e tests (skip without the optional extra installed)
  benchmarks/                               # 6 §9.1 perf gates (storage + aggregator + shim metadata/replay + report + smells)
  fixtures/
    validation/                             # E6: hand-labelled corpus consumed by validate_attribution.py
    internal-smells/                        # E6: per-smell fixture preservation (logbook companion)
.github/workflows/ci.yml                    # unit + benchmark + attribution-validation on every PR
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
