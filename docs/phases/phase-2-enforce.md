# Phase 2 — Enforce

**Theme:** *Enforce contracts before waste happens. Stop being a passive profiler.*
**Status:** approved scope; entered only after Phase 1 go-signal.
**Weeks:** 20–32 (12 weeks).
**Companion docs:**
- [Roadmap §4](../roadmap-inkfoot.md#4-phase-2--enforce-weeks-2032)
- [Architecture §4.5, §4.6, §4.7](../architecture-inkfoot.md)

---

## 1. Outcome

Inkfoot is no longer just a profiler — it **enforces economics**.
**Token Contracts** ship as YAML: teams declare per-task budgets,
runtime ceilings, and cache-hit targets, and the library blocks
violations in runtime *and* in CI via `inkfoot contract check`. The
**modification policies** (`LazyToolExposure`, `CheapSummariser`)
that need framework-level control finally work, via the Phase-1
adapters. More providers land. **Cost-per-success** becomes the
headline metric in reports.

The narrative shift from Phase 1: in Phase 1 we *explained* what
happened. In Phase 2 we **prevent** it from happening again. This is
the phase that introduces the switching-cost moat — once a team has
50 contracts declared per-agent, per-task, per-tier, leaving Inkfoot
means re-implementing those contracts in a competitor's DSL.

## 2. What ships

| Deliverable | Architecture ref | Notes |
|---|---|---|
| **Token Contracts** — YAML schema + runtime enforcement | [§4.6](../architecture-inkfoot.md) | Per-task budgets, max LLM calls, max tool result tokens, cache-hit floor, degrade ladder |
| **`inkfoot contract draft --task ...`** | §4.6 | Generates a draft contract from observed history with p95+10% headroom |
| **`inkfoot contract check`** CI gate | §4.6 | Exit code 2 on violation; integrates with the Phase-1 `inkfoot diff` |
| `BudgetCap` upgraded from observe to enforce | §4.5, §4.6 | Triggers the contract's degrade ladder (warn → switch_to_cheap_model → block) |
| **Modification policy: `LazyToolExposure(stale_after_turns=N)`** | §4.5 | Requires framework adapter (Pattern C); fails at registration on Pattern A |
| **Modification policy: `CheapSummariser(threshold_tokens=N)`** | §4.5 | Compresses oversized tool results before re-feeding |
| Framework adapter for **Pydantic AI** | §4.1 | Real adoption among type-safety-conscious teams |
| Framework adapter for **CrewAI** | §4.1 | Multi-agent workflow market |
| Provider support: **Gemini** | §4.2 | Translator for Gemini's `usageMetadata`; cache_resource style; cost-competitive |
| Provider support: **AWS Bedrock** (Converse API for Claude + Llama + Titan + Mistral + Cohere) | §4.2 | Enterprise-credentials path; one provider class for the AWS catalogue |
| Provider support: **OpenAI-compatible** (vLLM, Together, Fireworks, Groq, Ollama) | §4.2 | Long tail in one shim; per-`base_url` capability overrides |
| **Cost-per-success reporting** promoted to headline metric | §4.7, §4.12 | New columns in `inkfoot report`; docs promote it as *the* number |
| `inkfoot tail` (live event tail) | §6.2 | Engineer ergonomics during development |
| **Postgres storage backend** | §4.3 | Multi-process / multi-host installs; same schema as SQLite |
| `inkfoot migrate --to postgres` | §6.2 | Smooth migration off SQLite |
| **Five additional cost smells** | §4.4 | Informed by Phase 0 + Phase 1 production data |
| `inkfoot.dev/insights` early posts | — | Anonymised case studies (opt-in customers; consent required) |

### Phase 2 cost smells

Phase 0 shipped five smells; Phase 2 adds five more, validated against
real production usage:

| Smell ID | What triggers it | Recommendation |
|---|---|---|
| `tool-schema-drift` | Tool schema fingerprint changes mid-run | Stabilise tool ordering; avoid mid-run tool additions |
| `cost-skewed-by-outlier` | A single run is >10× p50 of its task | Investigate outlier; possibly enforce `BudgetCap` |
| `unbounded-conversation-history` | Run carries >50k tokens of memory | Add memory compression; truncate older turns |
| `over-instrumented-retries` | SDK retries firing >3× per call on average | Tune backoff; circuit-break upstream |
| `summariser-not-firing` | Tool result tokens consistently > 2k but no summariser configured | Enable `CheapSummariser(threshold_tokens=1500)` |

## 3. What we deliberately do NOT build

- **Cloud infrastructure** — Phase 3.
- **Cost Replay Engine** — Phase 3.
- **Static analyzer** (`inkfoot lint`) — Phase 3.
- **Invoice reconciliation** — Phase 3.
- **TypeScript port** — Phase 4.
- **Cost Smell Library (community)** — Phase 4.
- **Anomaly-based alerting** — Phase 4 (threshold-based lands in
  Phase 3 with Cloud).
- **IAM / SSO / SOC 2** — Phase 5.

## 4. Architecture this phase exercises

Newly implemented vs Phase 1:

- **§4.5** — capability matrix now used in anger: modification policies
  land only behind framework adapters.
- **§4.6** — Token Contracts (YAML schema, runtime enforcement, CI
  gate, draft generation from history).
- **§4.7** — outcome tracking *capture* was Phase 0; *reporting
  promotion* is Phase 2.
- **§4.4** — five additional smells.
- **§4.3** — Postgres storage backend + migration tooling.

The architecture's provider matrix expands materially in Phase 2:
Anthropic + OpenAI (Phase 0) → +Gemini +Bedrock +OpenAI-compat. The
capability flags shipped in Phase 0's ledger design now actually
matter — Bedrock-Llama has no prompt caching, Gemini uses cache
resources, etc.

## 5. Definition of done

- [ ] Token Contracts work end-to-end: YAML → runtime enforcement →
      CI gate.
- [ ] All Phase 2 framework adapters (LangGraph, OpenAI Agents SDK,
      Anthropic SDK, Pydantic AI, CrewAI) pass the contract-test
      harness against real LLM APIs (CI runs weekly).
- [ ] All Phase 2 provider implementations (Gemini, Bedrock,
      OpenAI-compat) pass the contract-test harness.
- [ ] `LazyToolExposure` and `CheapSummariser` work end-to-end via
      framework adapters; refuse cleanly on Pattern A.
- [ ] Cost-per-success appears in `inkfoot report` and is the
      *promoted* headline number in docs.
- [ ] At least 10 external users have starred *and* opened a real
      issue or PR.
- [ ] Postgres backend has a migration path with documented runbook;
      SQLite → Postgres migration tested on a 100k-event corpus.
- [ ] `inkfoot contract draft` produces a sensible draft from a
      real-world fixture (≥ 100-run history).
- [ ] CI in this repo includes `inkfoot contract check` as a required
      gate.

## 6. Go/no-go signal — Phase 2 → Phase 3

Phase 2 transitions to Phase 3 (Cloud beta) **if at the 8-week mark
post-Phase-2 launch** all three signals are true:

- ≥ 2000 GitHub stars, AND
- ≥ 50 weekly active installs (PyPI estimate), AND
- ≥ 1 company has emailed asking about commercial options *("can we
  use this internally? do you offer support?")*

**Only two of three:** slow-roll Phase 3 and prioritise OSS
hardening — make sure adapters work cleanly across edge cases, add a
sixth+seventh adapter, deepen the recommendation engine. The Cloud
bet is premature without the OSS retention signal.

**None or one:** stop and reshape the product. Building Cloud for an
OSS user base that doesn't exist is the failure mode of every
OSS-to-SaaS pivot that didn't work.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **Token Contract DSL gets too complex.** Trying to express everything in YAML produces a not-quite-Turing-complete language people hate. | Constrain the schema strictly to the architecture's contract spec (§4.6); reject feature creep ("dynamic budgets based on time-of-day", "per-customer overrides") to Phase 3+ if at all. |
| **Cost-per-success promotion exposes our outcome-tagging adoption gap.** If most runs lack outcome tags, "cost per success" is undefined or worse. | Surface uninstrumented-outcome runs as a separate bucket in reports; warn-loudly in docs about needing the tag; provide a "set_outcome from heuristic" helper for common patterns (e.g., LangGraph `END` state → success). |
| **Five frameworks × three patterns × five providers = test surface explosion.** | Contract test harness: one parametrised matrix; mark slow-live tests as `@pytest.mark.live_<provider>` and run weekly. CI runs the matrix against `FakeLLMProvider` fixtures on every commit. |
| **`CheapSummariser` quality on tool outputs.** Bad summaries cost downstream accuracy. | Use Haiku (Anthropic) / gpt-4o-mini (OpenAI) / Flash (Gemini) per provider's `cheap_model_for_summariser` (a Sleuth architecture pattern — borrowed); fall back to mechanical truncation if no cheap model is declared; per-run summariser-quality metric. |
| **Postgres backend introduces a multi-process write race.** | The two-tier write semantics from Phase 0 already anticipate this; the aggregator is single-writer; multi-process is a Postgres-only path; SQLite stays single-process. |
| **Provider expansion vs maintenance burden.** Five providers in one phase is ambitious. | OpenAI-compat covers ~50 effective backends in one class; treat that as the long-tail solution. Direct integrations only for Anthropic + OpenAI + Bedrock + Gemini. |

## 8. Suggested epic breakdown (for later)

Prefix **EN** (Enforce). Suggested:

- **EN1** — Token Contract YAML schema + parser + validation.
- **EN2** — Runtime contract enforcement (degrade ladder; `warn` /
  `switch_to_cheap_model` / `block` actions; `contract_violation`
  events).
- **EN3** — `inkfoot contract draft` (history-based draft generation
  with p95+10% defaults).
- **EN4** — `inkfoot contract check` CI gate + integration with
  Phase-1 `inkfoot diff`.
- **EN5** — `LazyToolExposure` policy (Pattern C only).
- **EN6** — `CheapSummariser` policy (Pattern C only; per-provider
  summariser routing).
- **EN7** — Pydantic AI framework adapter.
- **EN8** — CrewAI framework adapter.
- **EN9** — Gemini provider (translator + `cache_resource` cache
  style + capability declaration).
- **EN10** — AWS Bedrock provider (Converse API; multi-model class).
- **EN11** — OpenAI-compatible provider (one class for vLLM /
  Together / Fireworks / Groq / Ollama; per-`base_url` capability
  override).
- **EN12** — Postgres storage backend + `inkfoot migrate --to
  postgres`.
- **EN13** — Five additional cost smells (Phase 2 list above).
- **EN14** — Cost-per-success report promotion (new columns;
  uninstrumented-runs bucket; docs update).
- **EN15** — `inkfoot tail` live event tail.

EN1 + EN2 + EN3 + EN4 are the contracts critical path. EN5 + EN6
unlock the modification-policy promise from Phase 1. EN9 + EN10 +
EN11 expand the provider matrix. EN12 + EN14 are the
single-developer-readable wins.

## 9. Open questions

- **Should contracts be per-task or per-(task, tenant) once
  multi-tenant lands in Phase 5?** Decision: per-task at the file
  level; tenant overrides are a Phase-5 layer that doesn't change
  the schema, only the resolution order.
- **What happens when the degrade ladder's `switch_to_cheap_model`
  fires on a provider that has no obvious cheap model in its family
  (e.g., a Bedrock-Mistral deployment)?** Decision: fall through to
  `block`; surface a clear log line; document.
- **Should `CheapSummariser` use the *current run's* provider or a
  separately-configured cheap-summary provider?** Decision: same
  provider (avoids credential proliferation; matches the multi-
  provider architecture's pattern). Override is possible per-policy
  for the edge case.
- **`inkfoot contract draft` and adversarial history.** If the
  observed history was already abnormal (e.g., one bad week
  inflated p95), the draft inherits the abnormality. Mitigation:
  surface the source window prominently; suggest narrower windows
  for noisy datasets.
