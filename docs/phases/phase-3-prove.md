# Phase 3 — Prove

**Theme:** *Prove savings against outcome quality and provider invoices. Build a business.*
**Status:** approved scope; entered only after Phase 2 go-signal.
**Weeks:** 32–48 (16 weeks).
**Companion docs:**
- [Roadmap §5](../roadmap-inkfoot.md#5-phase-3--prove-weeks-3248)
- [Architecture §4.9, §4.10, §4.13–§4.15](../architecture-inkfoot.md)

---

## 1. Outcome

Inkfoot Cloud launches in **private beta with 5–10 design partners**.
Three breakthrough capabilities ship simultaneously: the **Cost
Replay Engine**, the **static analyzer** (`inkfoot lint`), and
**invoice reconciliation**. We have at least **one paying customer**
by phase end.

The narrative shift: in Phase 2 we enforced contracts (preventing
known waste). In Phase 3 we **prove** savings against real provider
invoices and answer "what *would* this run have cost differently?"
honestly via measured replay rather than modelled estimates.

This is the phase where the business hypothesis is tested. Phases
0–2 were eight months of distribution investment; Phase 3 asks
whether anyone will pay.

## 2. What ships

### Cloud foundation

| Deliverable | Architecture ref | Notes |
|---|---|---|
| Cloud ingestion service `POST /api/v1/events` | [§4.14](../architecture-inkfoot.md), §6.3 | Per-tenant API keys; FastAPI + Postgres + Redis + workers (Sleuth-pattern reuse) |
| Cloud Postgres storage | §4.3, §4.14 | Same schema as local; tenant-scoped queries in app code (RLS deferred to Phase 5) |
| Cloud query API | §6.3 | Aggregates, run detail, event timeline, by-category breakdowns |
| Cloud dashboard frontend | §4.14 | Causal attribution charts, cost-per-task, cost-per-success, time-series, cost-driver attribution |
| **Cloud exporter** in the OSS library | §4.13 | Batch upload, fail-open, never blocks the agent thread |
| API key management UI | §4.14 | Create / rotate / revoke per workspace |
| Single-user-per-workspace auth | ADR-007 | API key only; full IAM is Phase 5 |
| Billing wiring (Stripe) | — | Per-event tier + seat fee |
| Pricing page on marketing site | — | Free / Pro / Team / Enterprise tiers |

### Three structural USPs (the headline ships)

| Deliverable | Architecture ref | USP rank |
|---|---|---|
| **Cost Replay Engine** — Cloud-only | §4.9, ADR-009 | USP 2 |
| **Static analyzer** (`inkfoot lint`) with 8 launch rules | §4.10, ADR-010 | USP 3 (with runtime + replay) |
| **Invoice reconciliation** for Anthropic + OpenAI | §4.15 | The finance-grade conversion hook |
| **FOCUS-spec export** (FinOps FOCUS CSV/Parquet) | §4.15 | Apptio / Vantage / CloudHealth ingest ready |
| Threshold-based alerting | §4.14 | "Cost-per-task exceeded $X" / "Cache hit rate below Y" |
| Cost attribution rules | §4.14 | Map `inkfoot.tag()` metadata to dashboard dimensions |

### Cloud pricing (strawman; design-partner iteration sets final numbers)

| Tier | Price | Limits |
|---|---|---|
| Free | $0 | 10k events/mo, 7-day retention, 1 workspace |
| Pro | $39/mo | 250k events/mo, 30-day retention, 3 workspaces, alerts, **invoice reconciliation** |
| Team | $249/mo | 2.5M events/mo, 90-day retention, unlimited workspaces, 5 seats, **Cost Replay Engine**, **static analyzer in CI** |
| Enterprise | Contact | Custom volume, custom retention, SSO, self-host option |

The Pro → Team upgrade is anchored on invoice reconciliation (see
your spend → reconcile against your provider invoice). Cost Replay
Engine sits at Team because it has real infrastructure costs (we
make real LLM calls during replay) and because it's the strongest
"wow" feature — premium-tier placement maintains the upgrade
incentive.

## 3. What we deliberately do NOT build

- **TypeScript port** — Phase 4.
- **Cost Smell Library (community contributions)** — Phase 4.
- **Anomaly-based alerting** — Phase 4. Threshold-based ships in
  Phase 3.
- **Slack / PagerDuty integrations** — Phase 4.
- **Multi-tenant IAM, SSO, SAML, RBAC** — Phase 5.
- **SOC 2 Type 2** — Phase 5.
- **Self-hosted Cloud distribution** — Phase 5.
- **EU data residency** — Phase 5.
- **Postgres RLS as defense-in-depth** — Phase 5.
- **Invoice reconciliation for Bedrock + Gemini** — Phase 4
  (Anthropic + OpenAI in Phase 3).

## 4. Architecture this phase exercises

Newly implemented vs Phase 2:

- **§4.9 Cost Replay Engine** — Cloud-side. The replay job loads an
  original run's events, applies a new policy stack, re-runs LLM
  turns against recorded tool fixtures, records the replay as a new
  run with `parent_run_id` pointing at the original, and flags
  divergence.
- **§4.10 Static analyzer** — read-only AST analysis of agent source
  code; 8 lint rules at launch (suggested set in §8 below).
- **§4.13 Cloud exporter** — the OSS-side background thread.
- **§4.14 Cloud backend** — ingestion + query + dashboard + alerting.
- **§4.15 Invoice reconciliation** — Anthropic + OpenAI billing API
  ingest; matched / unattributed / unobserved buckets; FOCUS export.

The architecture's **§9.3 Privacy** posture is load-bearing in this
phase: the OSS Cloud exporter ships metadata-only by default; users
who want content uploaded for replay must explicitly opt in.

## 5. Definition of done

- [ ] 5–10 design partners actively using Cloud in production.
- [ ] **One paying customer (Pro or Team) at phase end.**
- [ ] Cost Replay Engine works end-to-end: pick a run, change policy
      stack, get real cost comparison with divergence flag.
- [ ] Invoice reconciliation works for Anthropic + OpenAI; the
      unattributed-spend and unobserved-spend reports render with
      sensible UX.
- [ ] FOCUS-spec export validated against the FinOps FOCUS schema.
- [ ] Static analyzer: 8 lint rules; runs cleanly on the LangGraph,
      OpenAI Agents SDK, and Anthropic SDK reference repos.
- [ ] Median event ingestion latency < 500 ms.
- [ ] Dashboard p95 query latency < 2 s.
- [ ] 99.5% uptime over 4 consecutive weeks before phase exit.
- [ ] Stripe billing wired up; the $0 → $39 → $249 transition works
      end-to-end (free trial → paid).
- [ ] Cloud exporter in the OSS library fails open under simulated
      Cloud-unreachable conditions; local SQLite remains canonical.

## 6. Go/no-go signal — Phase 3 → Phase 4

Phase 3 transitions to Phase 4 (Compound) if all of:

- ≥ 3 paying customers at phase exit, AND
- Combined ARR > $5k (small but real; someone budgeted for this),
  AND
- ≥ 2 written testimonials describing what Inkfoot found that
  customers would not have found otherwise.

**If we have one paying customer but no others convert:** *value,
not viability.* Slow Phase 4, deepen Phase 3, investigate pricing or
positioning friction. The product works for the one customer; the
distribution to the next 30 doesn't.

**If we have zero paying customers:** the SaaS thesis is wrong.
Three options worth considering:

1. Stay an OSS project funded by consulting; pause Cloud.
2. Pivot to support contracts (annual contracts for Inkfoot OSS
   support, training, custom adapters).
3. Acquire-hire conversation with an observability vendor
   (Langfuse, Helicone, Datadog AI). See roadmap §12 for the exit
   landscape.

None of these is a failure; they're all healthy off-ramps.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **Replay Engine credential management goes wrong.** Replay requires customer LLM credentials. A leak here is fatal. | Per-tenant key vault (envelope encryption); never log keys; per-replay audit trail; borrow Sleuth's IAM credential design. Document threat model. |
| **Static analyzer is harder than scoped.** Robust AST analysis across multiple agent frameworks is a real research effort. | Ship 5 well-tuned rules first, not 50 mediocre ones. Phase 3 scope acceptable to slip by 4 weeks if rule quality is at stake. The "tool-schema-in-loop" rule alone is high-value. |
| **Provider invoice API instability.** Anthropic, OpenAI billing APIs may change shape mid-implementation. | Reconciliation API behind a versioned adapter; graceful degradation (show "could not reconcile" rather than break); contract tests against each provider weekly. |
| **Replay divergence framing erodes trust.** If 40% of replays diverge, the user thinks the feature is broken. | Lead with divergence as *signal*, not bug; the docs say "divergence means your agent picked different tools under the new policy, which is itself useful diagnostic information." Surface divergence rate as a corpus statistic. |
| **Cloud infrastructure cost exceeds revenue early.** A free-tier user uploading 10k events/mo on a $0 plan + the Postgres + Redis + workers cost = burn. | Strict free-tier limits; clear pricing-page math; design partners on Pro tier from day one (no free-tier design partners). |
| **Counterfactual simulation pressure from the market.** Competitors claim "simulation" features that are vaporware-or-modelled. Inkfoot's honest "replay > simulation" framing may sound timid. | Be loud about the honest-measurement framing in the launch story; show replay cost outputs side-by-side with the original; the trust differential compounds. |

## 8. Suggested epic breakdown (for later)

Prefix **PR** (Prove). Suggested:

- **PR1** — Cloud ingestion service (`POST /api/v1/events`,
  per-tenant API keys, schema validation, per-tenant rate limits).
- **PR2** — Cloud Postgres schema (mirror of local SQLite with
  tenant scoping; migration framework; aggregator workers).
- **PR3** — Cloud exporter in OSS (background thread, batched
  POST, fail-open behaviour, bounded queue).
- **PR4** — **Cost Replay Engine** core (load events, build replay
  context, apply policy stack, call LLM with customer credentials,
  divergence detection, replay-as-child-run persistence).
- **PR5** — Replay API endpoints (`POST /api/v1/replay`,
  `GET /api/v1/replay/{id}`, polling vs streaming, comparison
  rendering).
- **PR6** — Customer-credential vault (per-tenant envelope
  encryption; never-log; audit trail).
- **PR7** — **Static analyzer** (`inkfoot lint`) — 8 launch rules
  across LangGraph + OpenAI Agents SDK + Anthropic SDK source
  patterns.
- **PR8** — Static analyzer CI integration (`inkfoot lint` exit
  codes; complement `inkfoot diff`).
- **PR9** — **Invoice reconciliation** for Anthropic Usage API.
- **PR10** — Invoice reconciliation for OpenAI Usage API.
- **PR11** — Reconciliation report UX (matched / unattributed /
  unobserved buckets; per-line drill-down).
- **PR12** — FOCUS-spec export (FinOps FOCUS CSV/Parquet
  conformance).
- **PR13** — Cloud dashboard frontend (causal attribution, time-
  series, cost-per-task, cost-per-success, cost-driver attribution,
  by-tag rollups).
- **PR14** — Threshold-based alerting (rule definition, evaluation
  worker, delivery via email; Slack/PagerDuty land in Phase 4).
- **PR15** — Billing wiring (Stripe; per-event tier + seat fee;
  free → Pro → Team upgrade flow).
- **PR16** — API key management UI (workspace-level create / rotate
  / revoke).
- **PR17** — Marketing site + pricing page.
- **PR18** — Design-partner onboarding playbook (no formal sales;
  founder-led; weekly checkin; clear success metric).

### Static analyzer launch rules (suggested 8)

1. `tool-schema-in-loop` — tool definitions constructed inside the
   agent loop body. Cache-breaker. **Critical.**
2. `system-prompt-timestamp` — `time.time()` / `datetime.now()`
   inside a string that lands in the system message. **Critical.**
3. `mutable-system-prefix` — system message built from f-string
   with run-varying inputs. **Warn.**
4. `unbounded-retry-loop` — `while True:` over LLM calls with no
   cap or backoff. **Critical.**
5. `tool-result-without-size-check` — tool result passed to model
   with no length check or summariser. **Warn.**
6. `model-from-user-input` — model parameter derived from user
   input (cache-breaker + safety risk). **Critical.**
7. `tools-added-mid-conversation` — tools list mutated inside the
   loop. **Warn.**
8. `missing-outcome-tag` — `@agent_run` decorator present but no
   `set_outcome()` call in the function body. **Info.**

The PR breakdown above is suggestive; the actual epic doc lives in
`epics-phase-3-prove.md` (to be drafted when this phase enters
execution).

## 9. Open questions

- **Pricing.** The $39 / $249 numbers are strawman. Design-partner
  willingness-to-pay sets the real ones. The doc should be updated
  with the real prices before Phase 3 launch.
- **Replay-engine cost attribution.** Replays cost real LLM money on
  our infrastructure. Is that a per-replay-priced feature on top of
  the tier? Or absorbed into the Team tier as part of the upgrade
  hook? Default: absorbed, with a per-tenant monthly replay-spend
  cap.
- **Static analyzer cross-framework consistency.** Each framework
  has slightly different idioms. Do we ship per-framework rule sets
  or a unified set with per-framework configuration? Default:
  unified rule set; per-framework configuration.
- **Invoice reconciliation data freshness.** Provider billing APIs
  often lag by 24–48 hours. Should reconciliation be "yesterday's
  spend" or live? Default: explicit "as of date" surfaced
  prominently; never live.
- **Customer-credential storage.** Do design-partner customers
  trust Inkfoot Cloud with LLM credentials in Phase 3, before SOC 2
  exists (Phase 5)? Default: yes for design partners (they're
  explicit early-adopters); SOC 2 unblocks the next tier of
  customers in Phase 5.
