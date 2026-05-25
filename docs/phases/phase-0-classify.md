# Phase 0 — Classify

**Theme:** *Classify where every billed token came from. Run on our own agents.*
**Status:** approved scope; first phase of execution.
**Weeks:** 0–8.
**Companion docs:**
- [Roadmap §2](../roadmap-inkfoot.md#2-phase-0--classify-weeks-08) —
  the strategic case for this phase.
- [Architecture §4.1–§4.7, §4.12](../architecture-inkfoot.md) — the
  technical sections this phase implements.

---

## 1. Outcome

A working Inkfoot Python library that:

- Monkey-patches the Anthropic and OpenAI SDKs (Pattern A) and
  attributes every billed token to one of 13 categories in the
  **Causal Token Ledger**.
- Persists events to local SQLite at `~/.inkfoot/runs.db` with
  two-tier write semantics (synchronous `runs.status`; eventually-
  consistent aggregate projections).
- Captures **outcome tags** (`success` / `failure` / `human_escalated`)
  via an explicit `inkfoot.set_outcome(...)` call.
- Surfaces five built-in cost smells automatically when the user runs
  `inkfoot report`.
- Enforces three **observation policies** at the instrumentation
  boundary (`BudgetCap`, `RetryThrottle`, `CacheControlPlacer`).
- Fails loudly at registration when the user asks for a policy that
  Pattern A can't honour (`PolicyNotSupported`).

**The wall-time deliverable:** our own internal agents (Sleuth
investigations and our internal tooling) run with Inkfoot in production
for **six consecutive weeks** before we tell anyone else this exists.
Phase 0 is **internal use only.**

The end-of-phase artefact is not a release; it's **a written list of
real cost smells we encountered in our own data** that we would not
have found otherwise. This list seeds Phase 2's smell additions.

## 2. What ships

| Deliverable | Architecture ref | Notes |
|---|---|---|
| Pattern A SDK shim for `anthropic` (latest stable) | [§4.1](../architecture-inkfoot.md) | Auto-detects installed SDKs |
| Pattern A SDK shim for `openai` (latest stable) | §4.1 | Same shim pattern |
| Causal Token Ledger — 13-category attribution | §4.2 | Load-bearing data model |
| `NeutralCall` and `Run` dataclasses | §4.2 | One ledger per call; aggregate per run |
| Provider-native cache accounting (Anthropic + OpenAI) | §4.2, ADR-006 | Separate `cache_creation_tokens` and `cache_read_tokens`; never collapsed |
| Local SQLite storage (`~/.inkfoot/runs.db`) | §4.3 | WAL mode; pluggable Storage interface |
| Two-tier write semantics (`status` synchronous; aggregates eventually consistent) | §4.3, ADR-003 | `aggregates_dirty` flag; `inkfoot rebuild-aggregates` |
| Five built-in cost smells: `unstable-prompt-prefix`, `runaway-retry-loop`, `oversized-tool-result-recycled`, `expensive-model-low-entropy`, `recurring-cache-writes` | §4.4 | Data-driven rules; not code branches |
| Outcome tracking — `inkfoot.set_outcome(outcome, quality_score)` | §4.7 | Phase-0 primitive; reporting in Phase 2 |
| Three observation policies — `BudgetCap`, `RetryThrottle`, `CacheControlPlacer` | §4.5 | Capability matrix enforced |
| `PolicyNotSupported` at registration for unsupported policies | §4.5, ADR-013 | Fail-loud not fail-silent |
| `inkfoot report` CLI with attribution bar chart | §4.12 | Per-run + aggregate; the headline UI surface |
| Pricing table for Anthropic + OpenAI; nanodollar storage | ADR-008 | Integer nanodollars; never floats |
| `inkfoot rebuild-aggregates` recovery command | §4.3, ADR-003 | Recomputes projections from event log |
| `inkfoot tag(key, value)` and `inkfoot tag_retrieval(text)` | §4.2, §6.1 | Opt-in attribution hints |

## 3. What we deliberately do NOT build

- **Cloud exporter / Cloud backend** — Phase 3.
- **Framework adapters** (LangGraph, OpenAI Agents SDK, etc.) — Phase 1.
- **`inkfoot diff` and `inkfoot benchmark`** — Phase 1.
- **OpenTelemetry ingest/export** — Phase 1.
- **Token Contracts** — Phase 2.
- **Modification policies** (`LazyToolExposure`, `CheapSummariser`) —
  require framework adapters; ship in Phase 2.
- **Gemini / Bedrock / OpenAI-compatible providers** — Phase 2.
- **Cost Replay Engine** — Phase 3.
- **Static analyzer** — Phase 3.
- **Invoice reconciliation** — Phase 3.
- **TypeScript port** — Phase 4.
- **Cost Smell Library (community)** — Phase 4.
- **IAM / SSO / SOC 2** — Phase 5.

## 4. Architecture this phase exercises

The architecture sections actively implemented in Phase 0:

- **§4.1 Instrumentation layer** — Pattern A only.
- **§4.2 Causal Token Ledger** — all 13 categories; all six provider-
  shape recipes for the two supported providers.
- **§4.3 Storage layer** — SQLite backend + Storage interface; both
  consistency tiers.
- **§4.4 Recommendation engine** — the engine itself + five built-in
  smells.
- **§4.5 Policy engine** — capability matrix + three observation
  policies + `PolicyNotSupported`.
- **§4.7 Outcome tracking** — capture only; reporting promotion in
  Phase 2.
- **§4.12 Report renderer** — `inkfoot report` only; other commands
  stub-or-deferred.

The architecture sections **not** exercised in Phase 0:

- §4.6 Token Contracts (Phase 2).
- §4.8 `inkfoot diff` (Phase 1).
- §4.9 Cost Replay Engine (Phase 3).
- §4.10 Static analyzer (Phase 3).
- §4.11 OpenTelemetry (Phase 1).
- §4.13 Cloud exporter (Phase 3).
- §4.14 Cloud backend (Phase 3).
- §4.15 Invoice reconciliation (Phase 3).
- §4.16 Cost Smell Library (Phase 4).

## 5. Definition of done

- [ ] `pip install inkfoot` works from a private PyPI index.
- [ ] Pattern A instrumentation works for both `anthropic` and
      `openai` SDKs with no user-code changes beyond
      `inkfoot.instrument()`.
- [ ] All 13 Causal Token Ledger categories populate correctly for
      both providers; estimation flags surface for fields that
      required estimation.
- [ ] Outcome tagging round-trips: `set_outcome("success", 0.94)` →
      visible in `inkfoot report`.
- [ ] Five built-in smells fire on a synthetic fixture suite.
- [ ] `BudgetCap`, `RetryThrottle`, `CacheControlPlacer` work
      end-to-end; `LazyToolExposure` registration raises
      `PolicyNotSupported`.
- [ ] Local SQLite write < 1 ms at p95 (CI benchmark).
- [ ] Instrumentation overhead < 100 µs at p95 (CI benchmark).
- [ ] Causal attribution accuracy verified against hand-labelled
      runs: average per-category error < 10%.
- [ ] Our own production agents (Sleuth + internal tooling) have run
      on Inkfoot for ≥ 6 weeks.
- [ ] A written list of ≥ 3 real cost smells encountered in our own
      data exists, with the underlying runs preserved as fixtures.
- [ ] `inkfoot rebuild-aggregates` recovers projections from the
      event log on a corrupted-aggregates fixture.

## 6. Go/no-go signal — Phase 0 → Phase 1

Phase 0 transitions to Phase 1 (public OSS launch) **if and only if**
both signals are true at the 6-week internal-usage mark:

1. **Inkfoot has surfaced ≥ 3 real cost issues** in our own agents
   that we would not have found without it.
2. **The team voluntarily reaches for `inkfoot report`** when
   investigating a bill anomaly, instead of writing a one-off SQL
   query.

If neither: **the product premise is wrong.** Reshape (different
abstraction layer) or abandon. Do not invest Phase 1 effort building
a public-facing thing for a problem nobody on the building team
actually has.

If one of two: pause Phase 1, dig into why the other half didn't
materialise. Sometimes it's a reporting-UX gap (smells exist but the
report doesn't make them visible); sometimes it's a real product
premise gap.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **Causal attribution accuracy < 90%.** Several ledger categories require tokeniser-accurate estimation (`tool_schema_tokens`, `system_static_tokens`). If estimates are wrong by >10%, customers lose trust before they form it. | Phase 0 includes a hand-labelled validation corpus; estimation flags surface explicitly in reports; abandon if accuracy stays <90% after tuning. |
| **The team's own agents don't surface enough cost smells** to validate the premise. | Pre-defined "≥ 3 real cost issues" criterion; commit to abandoning if not met. Phase 0 ending with "we found nothing interesting" is a valid (and cheap) outcome. |
| **SQLite write performance under realistic load.** | CI benchmark with 10k events/run on a realistic agent fixture; WAL mode tuning; fail-fast if p95 > 1 ms. |
| **Anthropic / OpenAI SDK churn** mid-phase. | Pin SDK versions in the shim; track deprecation notices; treat SDK upgrade as its own small project. |
| **Outcome-tagging adoption friction even internally** — the team forgets to call `set_outcome()`. | Report surfaces "uninstrumented runs" as a separate bucket so the gap is visible to us first; if we don't tag, downstream metrics are honestly absent rather than misleadingly missing. |

## 8. Suggested epic breakdown (for later)

When this phase is approved for execution, the epic breakdown will
land as `epics-phase-0-classify.md`. Suggested epics with prefix
**CL** (Classify):

- **CL1** — Project scaffolding (Python package layout; `inkfoot.dev`
  domain reservation; PyPI name reservation; nanodollar money type).
- **CL2** — Storage foundation (SQLite schema; two-tier write
  semantics; `Storage` interface; `inkfoot rebuild-aggregates`).
- **CL3** — Causal Token Ledger (the 13 categories + `NeutralCall` +
  per-provider attribution recipes for Anthropic and OpenAI).
- **CL4** — Pattern A SDK shims (anthropic + openai monkey-patches;
  hook isolation so exceptions never reach user code).
- **CL5** — Policy engine + capability matrix (the ABC; three
  observation policies; `PolicyNotSupported` at registration).
- **CL6** — Outcome tracking (`set_outcome` / `tag` / `tag_retrieval`;
  outcome event; reporting deferred to Phase 2).
- **CL7** — Recommendation engine + five built-in smells.
- **CL8** — `inkfoot report` CLI with attribution bar chart.
- **CL9** — Internal-use rollout (instrument Sleuth + internal
  tooling; six-week production exposure; fixture preservation).
- **CL10** — Validation harness (hand-labelled run corpus; per-category
  accuracy measurement; estimation-flag audit).

CL1 + CL2 + CL3 + CL4 are the foundation; CL5–CL8 are the
user-facing surface; CL9 + CL10 are the go/no-go gates. CL9 is the
single longest-duration epic (six calendar weeks of production
exposure).

## 9. Open questions

- **Which agent should be the canonical "first instrumented agent"
  internally?** Sleuth investigations have a representative agent
  loop with tool use; the internal tooling agent is shorter-lived
  and may not exercise enough categories. Tentative: both, but
  Sleuth investigations carry the bulk of the validation weight.
- **Tokeniser approach for `tool_schema_tokens`.** `tiktoken` covers
  OpenAI well; Anthropic has its own tokeniser but it's not always
  available offline. Phase 0 acceptable workaround: best-effort
  with the closest available tokeniser + explicit
  `estimation_flags=["tool_schema_tokens"]`.
- **Should `BudgetCap` enforce in process or just observe?** Phase 0
  position: observe + log, do not interrupt. The interrupt path
  arrives with Token Contracts in Phase 2 where it has the right
  semantics (`degrade` action ladder).
