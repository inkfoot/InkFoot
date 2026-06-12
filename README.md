# Inkfoot

> **Find the hidden cost trail in every AI agent run.**

Inkfoot is a causal token economics layer for LLM agents. It instruments
existing agent frameworks (LangChain, LangGraph, OpenAI Agents SDK,
Anthropic Agent SDK, Pydantic AI, CrewAI) without requiring rewrites, attributes every
billed token to one of 13 causal categories, surfaces named cost smells
automatically, enforces declarative Token Contracts in runtime and CI,
and (in Cloud) replays past runs under different policies to prove
savings against real provider invoices.

**Status:** Inkfoot includes the SDK shims, the 14-field Causal
Token Ledger, the smell engine, the report CLI, the validation
harness, performance gates, framework adapters, CI cost-review
workflow, and bidirectional OpenTelemetry GenAI compatibility.
`inkfoot.langgraph.instrument(graph)` gives per-node attribution
via `inkfoot report --run <id> --group-by node`. `inkfoot
benchmark` runs scenario suites and emits a stable JSON artefact;
`inkfoot diff` compares two artefacts and produces a Markdown PR
comment + JSON report with an `ok|warn|fail` verdict and exit
codes; the composite `inkfoot/diff-action` GitHub Action wraps
both behind a one-line workflow step with a sticky PR comment.
`inkfoot.instrument(otel_ingest_port=4318)` opens a stdlib-only
OTLP/JSON receiver that translates `gen_ai.*` spans into
Inkfoot's 14-field ledger (deduplicated against the native shim),
and `inkfoot.instrument(otel_export_endpoint=...)` mirrors every
`llm_call` event back out as an OTel span (smells / outcomes as
logs) to any collector.

The user-facing surface today: `inkfoot.instrument()` to monkey-
patch the SDKs (and auto-register the LangChain callback handler
when `langchain-core` is importable — one handler covers
ChatAnthropic, ChatOpenAI on both the Chat Completions and
Responses APIs, AzureChatOpenAI, ChatGoogleGenerativeAI, and
ChatBedrock via LangChain's normalised `usage_metadata`, with
response-id dedup against the raw-SDK shims),
`@inkfoot.agent_run(task=...)` decorator + context
manager for run scoping, `inkfoot.set_outcome / tag / tag_retrieval
/ tag_node / checkpoint / report_cost` for in-run metadata,
`inkfoot.langgraph.instrument(graph)` /
`inkfoot.openai_agents.instrument(agent)` /
`inkfoot.anthropic_agent.instrument(agent)` /
`inkfoot.pydantic_ai.instrument(agent)` /
`inkfoot.crewai.instrument(crew)` for framework
adapters (per-node attribution, tool-dispatch events, per-agent /
per-task crew attribution),
the rule-based smell engine with eleven built-in cost smells, and
the `inkfoot` CLI with `report` (single-run attribution bar chart +
smells, or aggregate `--last 7d --group-by task` /
`--group-by tag.<key>` with cost-per-success /
cost-per-accepted-answer / avg_$ / p95_$ / success%, or single-run
`--group-by node` / `--group-by metadata.<key>` for per-node and
per-agent ledger totals), `tag`
(late tagging), `rebuild-aggregates`, `benchmark` (scenario
runner emitting the benchmark JSON artefact), and `diff`
(structured comparison between two artefacts with `ok/warn/fail`
verdicts and `0/1/2` exit codes for CI).
Under it: nanodollar money type, SQLite storage with WAL + two-tier
writes, claim-and-project aggregator, an optional Postgres backend
(`inkfoot[postgres]`) with an advisory-lock aggregation worker and
a resumable `inkfoot migrate --to postgres` cutover, the 14-field
Causal Token Ledger, per-provider Anthropic + OpenAI + Gemini translators with
stable-prefix detection, the capability-declaring provider layer
(Anthropic, OpenAI, Gemini, Bedrock, OpenAI-compatible),
`tiktoken`-based tokenisers with estimation flags, the pricing
module, and the three observation policies (`BudgetCap`,
`RetryThrottle`, `CacheControlPlacer`). Six perf gates run on
every PR.

## Quickstart (development)

Requires Python 3.10+.

```bash
git clone https://github.com/inkfoot/inkfoot.git
cd inkfoot
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
/ 3.12, runs the Postgres-backed integration suite against a
`postgres:16` service container, and uploads the storage benchmark
JSON as an artefact per PR.

## Repository layout

```
inkfoot/                                    # the Python package
  __init__.py                               # public re-exports (frozen surface)
  _version.py                               # SemVer skeleton
  _instrument.py                            # inkfoot.instrument() entry point — leading underscore so the submodule doesn't shadow the public callable on the package
  _run_context.py                           # ContextVar-based active-run pointer
  _shim_install.py                          # SDK auto-detect + install/uninstall
  errors.py                                 # InkfootError, PolicyNotSupported, ...
  money.py                                  # Nanodollar integer money type
  ledger.py                                 # 14-field CausalTokenLedger + invariant
  pricing.py                                # PRICING_ND_PER_TOKEN + estimate_nanodollars
  run.py                                    # Run + InMemoryRunState dataclasses
  tokenisers.py                             # tiktoken + Anthropic fallback
  normalise/
    __init__.py                             # NeutralCall + stable-prefix detector
    anthropic.py                            # AnthropicTranslator
    gemini.py                               # GeminiTranslator
    langchain.py                            # LangChainTranslator (usage_metadata → ledger)
    openai.py                               # OpenAITranslator
  providers/                                # provider capability + usage-mapping layer
    __init__.py                             # public provider re-exports
    base.py                                 # LLMProvider + Capabilities + TokenUsage
    _registry.py                            # ProviderRegistry singleton (auto-seeds the shim-backed providers)
    anthropic.py                            # AnthropicProvider
    openai.py                               # OpenAIProvider
    gemini.py                               # GeminiProvider (cache-resource aware)
    bedrock.py                              # BedrockProvider (per-model-family capabilities)
    openai_compat.py                        # OpenAICompatProvider (instance-configured compat endpoints)
  policy/
    __init__.py                             # Policy ABC + IntegrationPattern + register_policies
    registry.py                             # PolicyRegistry singleton
    budget_cap.py                           # BudgetCap observe-only policy
    retry_throttle.py                       # RetryThrottle
    cache_control_placer.py                 # CacheControlPlacer (Anthropic)
  shims/
    _isolation.py                           # @isolated_hook / safely_run
    _emit.py                                # shared event-emit pipeline
    anthropic.py                            # AnthropicShim (sync + async)
    gemini.py                               # GeminiShim (generate_content sync + async)
    openai.py                               # OpenAIShim (sync + async)
  adapters/                                 # framework adapters
    __init__.py                             # FrameworkAdapter Protocol re-export
    base.py                                 # FrameworkAdapter + Instrumentation Protocols
    _registry.py                            # AdapterRegistry singleton + get_active_adapter
    langgraph.py                            # LangGraph adapter (per-node attribution + tools fingerprint)
    openai_agents.py                        # OpenAI Agents SDK adapter (tool-dispatched events)
    anthropic_agent.py                      # Anthropic Agent SDK adapter
    pydantic_ai.py                          # Pydantic AI adapter (run scoping + registered-tool events)
    crewai.py                               # CrewAI adapter (per-agent / per-task attribution, observation-only)
  langchain/                                # LangChain callback handler
    __init__.py                             # global handler registration (instrument/uninstrument)
    handler.py                              # InkfootCallbackHandler (BaseCallbackHandler)
  langgraph.py                              # Top-level convenience: inkfoot.langgraph.instrument(graph)
  openai_agents.py                          # Top-level convenience: inkfoot.openai_agents.instrument(agent)
  anthropic_agent.py                        # Top-level convenience: inkfoot.anthropic_agent.instrument(agent)
  pydantic_ai.py                            # Top-level convenience: inkfoot.pydantic_ai.instrument(agent)
  crewai.py                                 # Top-level convenience: inkfoot.crewai.instrument(crew)
  smells/
    __init__.py                             # CostSmell + DetectionResult + DEFAULT_SMELLS + registry
    engine.py                               # SmellEngine (lazy, off the hot path)
    _helpers.py                             # event-payload parsing + pricing lookups
    unstable_prompt_prefix.py               # built-in smells, one per file
    runaway_retry_loop.py
    oversized_tool_result_recycled.py
    expensive_model_low_entropy.py
    recurring_cache_writes.py
    summariser_quality_regression.py
    tool_schema_drift.py
    cost_skewed_by_outlier.py
    unbounded_conversation_history.py
    over_instrumented_retries.py
    summariser_not_firing.py
  storage/
    __init__.py                             # Storage Protocol (lazy SQLiteStorage / PostgresStorage)
    sqlite.py                               # SQLiteStorage + WAL pragmas + replay-mode write
    migrations.py                           # forward-only DDL (schema v1)
    aggregator.py                           # claim-and-project AggregatorWorker
    postgres.py                             # PostgresStorage — psycopg pool ([postgres] extra)
    postgres_migrations.py                  # forward-only Postgres DDL (advisory-lock guarded)
    postgres_aggregator.py                  # per-sweep advisory lock + heartbeat for the worker
  _run_lifecycle.py                         # @agent_run + set_outcome/tag/tag_retrieval/report_cost
  outcomes/
    _heuristics.py                          # set_outcome_from_heuristic — outcome inference from framework results
  reports/
    cost_per_success.py                     # cost-per-success / cost-per-accepted-answer rollup + uninstrumented bucket
    tag_groupby.py                          # tag-value bucketing for `--group-by tag.<key>`
  cli/
    main.py                                 # `inkfoot` entry point (report / rebuild-aggregates / tag)
    rebuild_aggregates.py                   # `inkfoot rebuild-aggregates`
    report.py                               # `inkfoot report` — bar chart + smells (renderer is pure)
    tag.py                                  # `inkfoot tag <run-id> <key> <value>` — late tagging
    aggregator_worker.py                    # `inkfoot aggregator-worker` — Postgres aggregation daemon
    migrate.py                              # `inkfoot migrate --to postgres` — resumable SQLite copy
scripts/
  validate_attribution.py                   # validation harness — fails CI when per-category mean error > 10%
  extract_run_fixtures.py                   # extractor for the validation corpus
tests/
  unit/                                     # unit tests
  integration/                              # per-framework e2e tests (skip without the optional extra installed)
  benchmarks/                               # performance gates (storage + aggregator + shim metadata/replay + report + smells)
  fixtures/
    validation/                             # hand-labelled corpus consumed by validate_attribution.py
    internal-smells/                        # per-smell fixture preservation
.github/workflows/ci.yml                    # unit + Postgres integration + benchmark + attribution-validation on every PR
```

## Operator notes

- **PyPI name:** `inkfoot` is the intended package name. The current
  release is an **early-access pre-release** (a PEP 440 `a`/`b`/`rc`
  version such as `1.0.0a1`) so an internal reference repo can
  exercise the capability set ahead of the public release. Install it
  with pip's pre-release flag — `pip install --pre inkfoot` (or pin
  the exact version, `pip install "inkfoot==1.0.0a1"`). Until then,
  install from source.
- **Domain:** `inkfoot.dev` is reserved for the public docs site.
- **Default DB path:** `~/.inkfoot/runs.db`. Override via the
  `INKFOOT_HOME` environment variable (parent dir) or pass an
  explicit `path=` to `SQLiteStorage`.
- **Aggregator poll interval:** defaults to 500 ms; override with
  `INKFOOT_AGGREGATOR_INTERVAL_MS=<int>`. Values under 10 ms are
  clamped to the 10 ms floor with a warning.
- **Postgres backend:** install `inkfoot[postgres]`, set
  `INKFOOT_PG_DSN` (pool sizing via `INKFOOT_PG_POOL_MIN` /
  `INKFOOT_PG_POOL_MAX`), and pass a `PostgresStorage` instance to
  `inkfoot.instrument(storage=...)`. Aggregation runs out of
  process — keep one or more `inkfoot aggregator-worker` daemons
  running. `inkfoot migrate --to postgres` copies an existing
  SQLite history over (resumable; renames the source to
  `.migrated`, never deletes it).

## Releasing (early access)

The project publishes an early-access pre-release to PyPI; the public
launch (blog post, GitHub mirror, marketplace polish) lands later. The
pipeline is two tag-driven workflows plus a guard script:

1. **Bump** `inkfoot/_version.py` to a PEP 440 pre-release (e.g.
   `1.0.0a1`) and commit.
2. **Tag** the commit to match — `git tag v1.0.0a1 && git push
   origin v1.0.0a1`. The tag glob in
   [`.github/workflows/release-prerelease.yml`](.github/workflows/release-prerelease.yml)
   matches `a`/`b`/`rc` tags only (a final `v1.0.0` tag is ignored
   on purpose).
3. The **guard** ([`scripts/check_prerelease_tag.py`](scripts/check_prerelease_tag.py))
   asserts the tag matches `_version.py` and is an actual
   pre-release, then the workflow builds an sdist + wheel and
   publishes via PyPI **Trusted Publishing** (OIDC — no API token
   in repo secrets).
4. After a successful upload the workflow creates a **GitHub
   pre-release** (`prerelease: true`) for the tag. That published-release
   event is what triggers
   [`.github/workflows/release-smoke.yml`](.github/workflows/release-smoke.yml),
   which installs the just-published version from PyPI into a clean
   `python:3.10/3.11/3.12-slim` container and runs the hello-world
   quickstart end-to-end. (You can also smoke an arbitrary version
   on demand via the workflow's `workflow_dispatch` input.)

Framework and provider extras ship alongside the release —
`pip install "inkfoot[langchain]"`, `[langgraph]`,
`[openai-agents]`, `[anthropic-agent]`, `[pydantic-ai]`,
`[crewai]`, the provider
SDK extras `[gemini]` and `[bedrock]`, the storage extra
`[postgres]`, or `[all]` for every one of them. A weekly
[live-tests workflow](.github/workflows/live-tests.yml) installs
each framework extra and provider SDK from PyPI and runs the
contract + integration suites against the real thing (the Ollama
leg exercises an OpenAI-compatible endpoint against a real local
model), and a weekly
[live-langchain workflow](.github/workflows/live-langchain.yml)
drives the callback handler against every LangChain partner
package and real endpoint, so upstream drift surfaces as a red
matrix leg (and a tracking issue) instead of a user bug report.

## License

Apache 2.0 — see [LICENSE](LICENSE).
