# Roadmap — Inkfoot

> **Inkfoot — Find the hidden cost trail in every AI agent run.**

**Status:** approved direction; phases 0–1 actively planned, later phases
indicative.
**Companion doc:** [architecture-inkfoot.md](architecture-inkfoot.md) —
the technical design this roadmap delivers.

---

## 0. Premise

The market for general LLM gateways and unified provider abstractions
is saturated (LiteLLM, Bifrost, Portkey, Helicone, TensorZero,
Cloudflare, Vercel, OpenRouter). The market for *agent-aware LLM
FinOps* — tools that understand the internal shape of agent loops
well enough to attribute spend and reduce it without hurting task
success — is **emerging but not empty**.

Honest competitive picture as of mid-2026:

- **Langfuse** — closest direct competitor; framework-agnostic
  observability with cost tracking.
- **LangSmith** — framework-locked to LangChain.
- **AgentCost / AgentMeter / FinOps LLM** — early-stage; positioning
  overlaps; execution still thin.
- **TensorZero / Portkey / Bifrost** — gateway-first; cost is one of
  many concerns.

What none of them has is **causal attribution + prevention**:
classifying *where* the tokens came from, *explaining why* they were
spent, *enforcing contracts* before waste happens, and *proving
savings* against outcome quality and provider invoices.

The product premise:

> **Causal token economics for agents: classify where tokens came
> from, explain why they were spent, enforce contracts before waste
> happens, and prove savings against outcome quality and provider
> invoices.**

Each of the four verbs corresponds to a phase:

- **Phase 0 — Classify.** The Causal Token Ledger; instrument and
  attribute every billed token across 13 categories. Five built-in
  cost smells. Outcome tracking. Run on our own agents.
- **Phase 1 — Explain.** Public OSS launch. Recommendation engine in
  reports. `inkfoot diff` for CI cost review. PR comments.
  OpenTelemetry compatibility.
- **Phase 2 — Enforce.** Token Contracts (YAML, runtime + CI). More
  framework adapters. Cost-per-success reporting.
- **Phase 3 — Prove.** Cost Replay Engine. Static analyzer
  (`inkfoot lint`). Invoice reconciliation. Cloud beta launches.
- **Phase 4 — Compound.** TypeScript port. Cost Smell Library
  (community contributions with verification). Cloud GA.
- **Phase 5 — Enterprise.** SSO, self-hosted Cloud, SOC 2, EU.

The business model:

> **OSS profiler with local SQLite is free and useful on day one.
> Managed Cloud adds team dashboards, alerting, the Cost Replay
> Engine, invoice reconciliation, and long-term retention. CFOs and
> Heads of Engineering pay for Cloud; engineers pull the OSS in.**

This roadmap is structured around six phases, each with a defined
outcome, a definition-of-done, and a *go/no-go signal* that
determines whether the next phase is justified.

---

## 1. Strategic shape

```
Phase 0  ── Classify       (8 weeks)    ── internal use only
Phase 1  ── Explain        (12 weeks)   ── public OSS launch
Phase 2  ── Enforce        (12 weeks)   ── Token Contracts ship
Phase 3  ── Prove          (16 weeks)   ── Cloud beta + replay + reconcile + lint
Phase 4  ── Compound       (16 weeks)   ── Cloud GA + Smell Library + TS port
Phase 5  ── Enterprise     (ongoing)    ── SSO, self-hosted, EU, SOC 2
```

The phases are intentionally back-loaded toward Cloud. Phases 0–2 are
*eight months of investment in distribution* before the business
model engages in Phase 3. This is the OpenTelemetry / dbt / Helicone
pattern — build trust at the engineer-tooling layer before asking
finance teams for money.

Each phase has a hard go/no-go decision at its end. If a signal
doesn't materialise, the next phase is reshaped or abandoned. This is
not a 24-month commitment up front; it's six 2–4-month commitments,
each conditional.

---

## 2. Phase 0 — Classify (weeks 0–8)

### Outcome

Inkfoot instruments Anthropic + OpenAI calls, attributes every
billed token to one of 13 causal categories, captures outcome tags,
and surfaces five built-in cost smells automatically. We use it on
our own agents internally for 6+ weeks before showing anyone else.

### What we build

| Deliverable | Source |
|---|---|
| Instrumentation layer Pattern A (SDK shim) for Anthropic + OpenAI | architecture §4.1 |
| **Causal Token Ledger** — 13-category attribution | architecture §4.2 |
| Local SQLite storage with two-tier write semantics | architecture §4.3 |
| **Five built-in cost smells** | architecture §4.4 |
| Outcome tracking primitives (`set_outcome`, `quality_score`) | architecture §4.7 |
| Three observation policies: `BudgetCap`, `RetryThrottle`, `CacheControlPlacer` | architecture §4.5 |
| CLI: `inkfoot report` with attribution bar chart | architecture §4.12 |
| Pricing table for Anthropic + OpenAI; nanodollar storage | ADR-008 |
| Honest policy capability matrix; `PolicyNotSupported` at registration | ADR-013 |

### What we do NOT build

- Cloud exporter / Cloud backend (Phase 3).
- Framework adapters beyond the SDK shim (Phase 1).
- `inkfoot diff` and CI integration (Phase 1).
- Token Contracts (Phase 2).
- Cost Replay Engine (Phase 3).
- Static analyzer (Phase 3).
- Modification policies (`LazyToolExposure`, `CheapSummariser`) —
  they need framework adapters; ship with adapters in Phase 2.
- Gemini / Bedrock / others (Phase 2).

### Definition of done

- [ ] `pip install inkfoot` works from a private PyPI index.
- [ ] The repo's own agents (Sleuth investigations + internal
      tooling) run with Inkfoot in production for 6 weeks. Real
      data.
- [ ] **We have a written list of *cost smells we actually
      encountered* in our own data.** This list extends the
      Phase-0 five-smell baseline and seeds the Phase 2 / Phase 4
      additions.
- [ ] Causal attribution accuracy verified against hand-labelled
      runs: average per-category error < 10%.
- [ ] Instrumentation overhead < 100 µs at p95 (CI benchmark).
- [ ] Local SQLite write < 1 ms at p95 (CI benchmark).

### Go/no-go signal

The signal is *internal usage*. Phase 0 is a go to Phase 1 if at the
6-week mark:

1. **Has Inkfoot surfaced ≥ 3 real cost issues** in our own agents
   that we would not have found otherwise?
2. **Does the team voluntarily reach for `inkfoot report`** when
   investigating a bill anomaly, instead of writing a one-off
   script?

If yes, Phase 1 is justified. If no, the product is solving a
problem we don't have. We either reshape (different abstraction
layer) or abandon.

---

## 3. Phase 1 — Explain (weeks 8–20)

### Outcome

Inkfoot is on public PyPI with a credible README, a launch blog
post, OTel compatibility, framework adapters for the dominant agent
frameworks, and the `inkfoot diff` CI cost-review workflow. We're
present in the conversation about agent cost.

### What we build

| Deliverable | Notes |
|---|---|
| Adapter for **LangGraph** | Largest agent-framework market share as of mid-2026 |
| Adapter for **OpenAI Agents SDK** | Bound to OpenAI but big and growing |
| Adapter for **Anthropic Agent SDK** | Smaller but high-quality usage |
| Adapter for **raw `anthropic` / `openai` SDKs** | The "I rolled my own" market |
| **`inkfoot diff` and `inkfoot benchmark`** | CI cost review with PR-comment output |
| **GitHub Action wrapper** (`inkfoot/diff-action`) | One-line CI integration |
| **OpenTelemetry ingest + export** | GenAI semantic conventions; `inkfoot.*` extensions |
| Recommendation engine in reports | Smells from Phase 0 now surface inline in `inkfoot report` |
| Public docs site (`inkfoot.dev`) | Searchable; quickstart; recipes; API reference |
| Three published recipes | "Find your most expensive agent," "Spot cache-miss patterns," "Set up CI cost review" |
| Launch blog post | "We measured our own agents and learned X" — narrative, with real numbers |
| CI matrix across Python 3.10/3.11/3.12 | Mandatory OSS hygiene |
| Contribution guide + CoC + issue templates | Standard OSS hygiene |

### What we do NOT build

- Cloud infrastructure (Phase 3).
- Token Contracts (Phase 2).
- Cost Replay Engine (Phase 3).
- Static analyzer (Phase 3).
- Modification policies (Phase 2).
- TypeScript port (Phase 4).
- Pydantic AI / CrewAI adapters (Phase 2).

### Definition of done

- [ ] `pip install inkfoot` from public PyPI.
- [ ] Docs site live at `inkfoot.dev` (or similar).
- [ ] Launch blog post on a credible engineering blog.
- [ ] OTel ingest validated against a reference collector.
- [ ] At least one significant reach event: HN front-page submission,
      LangChain official roundup mention, Anthropic blog signal, or
      a PyCon/AI conference talk acceptance.
- [ ] Public GitHub mirror with Apache 2.0 license.
- [ ] **Three external users** outside our team with runs recorded
      > 7 days after install.

### Go/no-go signal

The signal is *organic adoption*. Phase 1 is a go to Phase 2 if at
the 8-week mark post-launch we have:

- ≥ 500 GitHub stars, OR
- ≥ 100 unique PyPI installs/day, OR
- ≥ 5 external contributors with real issue threads.

Hitting one is the bar. Hitting two is healthy. Hitting none means
the wedge isn't landing — reshape positioning, messaging, or the
entry point before continuing.

---

## 4. Phase 2 — Enforce (weeks 20–32)

### Outcome

Inkfoot is no longer just a profiler — it *enforces* economics.
Token Contracts ship; teams declare expectations as YAML and the
library blocks violations in runtime and CI. Modification policies
work via framework adapters. More providers supported. Cost-per-
success becomes the headline metric in reports.

### What we build

| Deliverable | Notes |
|---|---|
| **Token Contracts** | YAML schema; runtime enforcement; `inkfoot contract draft` |
| **Token Contract CI gate** | `inkfoot contract check`; exit code 2 on violation |
| Modification policies: **`LazyToolExposure`**, **`CheapSummariser`** | Require framework adapters; capability matrix enforced |
| Adapter for **Pydantic AI** | Real adoption among type-safety-conscious teams |
| Adapter for **CrewAI** | Multi-agent workflow buyers |
| Provider support: **Gemini** | Cost-competitive; long context |
| Provider support: **AWS Bedrock** (Converse API for Claude + Llama) | Enterprise gateway |
| Provider support: **OpenAI-compatible** (vLLM, Together, Fireworks, Groq, Ollama) | Long tail in one shim |
| **Cost-per-success reporting** | New columns in `inkfoot report`; promoted as headline metric |
| `inkfoot tail` (live event tail) | Engineer ergonomics |
| Postgres storage backend | Multi-process / multi-host installs |
| Migration tools: `inkfoot migrate --to postgres` | Smooth path off SQLite |
| 5 additional cost smells (from Phase 0 internal findings) | Reach 10 built-in smells |

### Recommendation engine — Phase 2 additions

Five built-in smells in Phase 0; five more land in Phase 2 (informed
by our own production usage). Suggested additions:

| Smell ID | What triggers it | Recommendation |
|---|---|---|
| `tool-schema-drift` | Tool schema fingerprint changes mid-run | Stabilise tool ordering; avoid mid-run tool additions |
| `cost-skewed-by-outlier` | Single run is >10× p50 of its task | Investigate outlier; possibly enforce `BudgetCap` |
| `unbounded-conversation-history` | Run carries >50k tokens of memory | Add memory compression; truncate older turns |
| `over-instrumented-retries` | SDK retries firing >3× per call on average | Tune backoff; circuit-break upstream |
| `summariser-not-firing` | Tool result tokens consistently > 2k but no summariser configured | Enable `CheapSummariser(threshold_tokens=1500)` |

### Definition of done

- [ ] Token Contracts work end-to-end: YAML → runtime enforcement →
      CI gate.
- [ ] All Phase 2 adapters pass contract-test harness against real
      LLM APIs (CI runs weekly).
- [ ] Cost-per-success appears in `inkfoot report` and is the
      promoted headline number in docs.
- [ ] At least 10 external users have starred *and* opened an issue
      or PR.
- [ ] Postgres backend has a migration path with documented runbook.

### Go/no-go signal

The signal is *retention and engagement*. Phase 2 is a go to Phase 3
(Cloud beta) if at the 8-week mark post-Phase-2 launch:

- ≥ 2000 GitHub stars, AND
- ≥ 50 weekly active installs (PyPI estimate), AND
- ≥ 1 company has emailed asking about commercial options ("can we
  use this internally? do you offer support?")

If only one or two are met, slow-roll Phase 3 and prioritise OSS
hardening. If none, stop and reshape the product — no point building
Cloud for an OSS user base that doesn't exist.

---

## 5. Phase 3 — Prove (weeks 32–48)

### Outcome

Inkfoot Cloud launches in private beta with 5–10 design partners.
Three breakthrough capabilities ship simultaneously: the **Cost
Replay Engine**, the **static analyzer**, and **invoice
reconciliation**. We have at least one paying customer by phase end.

### What we build

| Deliverable | Notes |
|---|---|
| Cloud ingestion service | `POST /api/v1/events`; per-tenant API keys |
| Cloud Postgres storage | Same schema as local; tenant-scoped queries in app code |
| Cloud query API | Aggregates, run detail, event timeline, by-category breakdowns |
| Cloud dashboard frontend | Causal attribution charts, cost-per-task, cost-per-success, time-series, cost-driver attribution |
| **Cost Replay Engine** | The replay engine and its API. Cloud-only. |
| **Static analyzer** (`inkfoot lint`) | Phase 3 deliverable; 8 lint rules at launch |
| **Invoice reconciliation** for Anthropic + OpenAI | Phase 3 deliverable; finance-grade |
| **FOCUS-spec export** | FinOps FOCUS format; CSV/Parquet |
| Alerting (threshold-based) | "Cost-per-task exceeded $X" / "Cache hit rate below Y" |
| Cost attribution rules | Map `inkfoot.tag()` metadata to dashboard dimensions |
| Cloud exporter in OSS library | Batch upload, fail-open, never blocks agent |
| API key management UI | Create/rotate/revoke per workspace |
| Single-user-per-workspace auth | API key only; full IAM is Phase 5 |
| Billing wiring (Stripe) | Per-event tier + seat fee |
| Pricing page on marketing site | Free / Pro / Team tiers |

### Cloud pricing (proposed; iterate with design partners)

| Tier | Price | Limits |
|---|---|---|
| Free | $0 | 10k events/mo, 7-day retention, 1 workspace |
| Pro | $39/mo | 250k events/mo, 30-day retention, 3 workspaces, alerts, **invoice reconciliation** |
| Team | $249/mo | 2.5M events/mo, 90-day retention, unlimited workspaces, 5 seats, **Cost Replay Engine**, **static analyzer in CI** |
| Enterprise | Contact | Custom volume, custom retention, SSO, self-host option |

Pricing is the *most-iterated thing in Phase 3*. The numbers above
are strawman; design-partner conversations set the real ones.

**Note on tiering:** the Pro → Team upgrade is anchored on invoice
reconciliation (Pro: see your spend; Team: reconcile against your
provider invoice). This is the finance-conversation hook. Cost
Replay Engine sits at the Team tier because it has real
infrastructure costs (we make LLM calls during replay) and because
it's the strongest "wow" feature — keeping it premium maintains the
upgrade incentive.

### Definition of done

- [ ] 5–10 design partners actively using Cloud in production.
- [ ] One paying customer (Pro or Team) at phase end.
- [ ] Cost Replay Engine works end-to-end: pick a run, change policy
      stack, get real cost comparison with divergence flag.
- [ ] Invoice reconciliation works for Anthropic + OpenAI;
      unattributed-spend and unobserved-spend reports render.
- [ ] Static analyzer: 8 lint rules; runs in CI on the LangGraph,
      OpenAI Agents SDK, Anthropic SDK reference repos cleanly.
- [ ] Median event ingestion latency < 500 ms.
- [ ] Dashboard p95 query latency < 2 s.
- [ ] 99.5% uptime over 4 consecutive weeks before phase exit.

### Go/no-go signal

The signal is *willingness to pay*. Phase 3 is a go to Phase 4 if:

- ≥ 3 paying customers at exit, AND
- Combined ARR > $5k (small but real; someone budgeted for this), AND
- ≥ 2 written testimonials describing what Inkfoot found that
  customers would not have found otherwise.

If we have one paying customer but no others convert, we're in a
position of *value, not viability* — slow Phase 4, deepen Phase 3,
investigate pricing or positioning friction.

If we have zero paying customers, the SaaS thesis is wrong. Options:
stay an OSS project funded by consulting; pivot to support contracts;
acquire-hire conversation with an observability vendor.

---

## 6. Phase 4 — Compound (weeks 48–64)

### Outcome

Cloud is publicly available with self-serve signup. The Cost Smell
Library opens to community contribution with verified savings data.
TypeScript port lands. We have 15–30 paying customers and a clear
story for what's next.

### What we build

| Deliverable | Notes |
|---|---|
| **Cost Smell Library** | Community-contributed smells with verification data; `library.inkfoot.dev` |
| Smell contribution workflow | PR-based; review by core team; verification against opted-in customer corpus |
| Private smells | Customers can author private smells for their own use |
| **TypeScript port** (`@inkfoot/sdk`) | Pattern A (SDK shim), Pattern B (decorator), Pattern C adapters for Vercel AI SDK, OpenAI Node, Anthropic TS |
| Cloud public signup | Self-serve, Stripe billing, no sales call for Pro |
| Anomaly-based alerting | "3σ deviation from baseline" |
| Slack + PagerDuty integrations | Alert delivery |
| Cloud cost attribution v2 | Per-customer-attribute rollups, cohort analysis, percentile breakdowns |
| Public status page | Standard SaaS hygiene |
| Public roadmap site | OSS contributors and Cloud customers see + vote on what's coming |
| Inbound marketing blog | 5 published posts on FinOps patterns |
| `inkfoot.dev/insights` | Anonymised cost-smell case studies (with permission) |
| Invoice reconciliation extended to **AWS Bedrock + Gemini** | Phase 3 covered Anthropic + OpenAI; Phase 4 extends |
| Static analyzer extended to TypeScript | TS lint rules; LangChain.js and Vercel AI SDK code patterns |

### Definition of done

- [ ] TypeScript SDK on npm; parity with Python's Pattern A + B.
- [ ] Cost Smell Library has ≥ 20 community-contributed smells with
      verification data.
- [ ] ≥ 15 paying customers across Pro / Team.
- [ ] $20k+ MRR.
- [ ] Inkfoot cited in at least one external article or conference
      talk we did not write or organise.
- [ ] OSS adoption ≥ 200 weekly active installs.

### Go/no-go signal

The signal is *growth trajectory*. Phase 4 is a go to Phase 5
(Enterprise) if:

- MRR growing MoM at ≥ 15%, AND
- ≥ 1 customer at Team tier (≥ $249/mo) for ≥ 3 consecutive months,
  AND
- Sales conversations include enterprise-adjacent companies
  (Series B+ with > 30 engineers, or Fortune-2000 LOBs)

If growth is flat, stay self-serve and double down on product. If
growth is strong and enterprises ask for self-hosted / SSO, Phase 5
is justified.

---

## 7. Phase 5 — Enterprise (weeks 64+)

### Outcome

Inkfoot is a credible enterprise procurement candidate. SSO works.
Self-hosted Cloud ships. EU region is live. We have at least one
Fortune-2000 logo or one $100k+ ARR contract.

### What we build

| Deliverable | Notes |
|---|---|
| Full multi-tenant IAM | Reuse Sleuth's IAM design (architecture-iam.md) |
| SSO via OIDC (Google, Azure Entra, Okta) | Procurement-blocker until shipped |
| SAML SSO | Enterprise tier; not Pro/Team |
| RBAC | Owner / Admin / Member / Viewer per workspace |
| Audit log (Cloud-side) | Compliance-grade; ≥ 1 year retention configurable |
| **SOC 2 Type 2** | Single biggest enterprise SaaS gate in this category |
| Self-hosted Cloud distribution | Docker Compose + K8s Helm; runs in customer VPC |
| EU data residency | Frankfurt or Ireland region |
| Postgres RLS | Defense-in-depth on tenant isolation |
| Dedicated solutions engineer playbook | Onboarding doc, pricing model, sample contracts |
| Annual contract billing flow | Invoiced; not Stripe-self-serve |

### Definition of done

- [ ] SSO works end-to-end with Google, Azure Entra, Okta.
- [ ] Self-hosted Cloud shipped to ≥ 2 customers in production.
- [ ] SOC 2 Type 2 audit passed.
- [ ] ≥ 1 customer paying ≥ $100k/year OR three customers paying
      ≥ $30k/year.

### Go/no-go signal

By Phase 5 the company-or-acqui-hire conversation is the focus, not
go/no-go. Continued investment is justified by the same signals as a
normal SaaS scale-up: net revenue retention, sales efficiency,
customer acquisition cost vs lifetime value. If those numbers are
healthy, raise a Series A. If flat, optimise for profitability or
position for acquisition.

---

## 8. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Phase 0 internal usage doesn't surface real value | Medium | High — kills project early | Pre-define "three real cost issues" criterion; commit to abandoning if not met |
| OSS launch (Phase 1) doesn't get traction | Medium | High | Multiple parallel launch channels (blog, HN, framework communities); pivot positioning if first wave flat |
| Adoption happens but conversion to Cloud doesn't | Medium | Medium-High | OSS-as-business is acceptable backup; sustain via support contracts; reshape Cloud pricing |
| **Causal attribution accuracy is too low** | Medium | High — undermines the entire pitch | Phase 0 includes a large-corpus validation against hand-labelled runs; estimation flags surfaced explicitly; abandon if accuracy stays < 90% |
| An incumbent (Langfuse, Helicone, LangSmith) ships causal attribution first | Medium | Medium | Speed; lean into Token Contracts + Replay + static analysis as the harder-to-copy combination |
| LangGraph / OpenAI Agents SDK / Anthropic SDK fragment or churn | High | Medium | Adapter design is small; SDK shim (Pattern A) is more durable than framework adapters |
| Vendor pricing changes break our table | High | Low | Quarterly review; CI watch; bundled snapshot + Cloud refresh |
| Privacy concerns block Cloud adoption | Medium | High | Default metadata-only posture; never upload prompt content unless opted in; SOC 2 by Phase 5 |
| Static analyzer is harder than scoped | Medium | Medium | Ship 5 well-tuned rules first, not 50 mediocre ones; Phase 3 scope acceptable to slip by 4 weeks |
| Replay Engine credential management goes wrong | Low | High | Borrow Sleuth's IAM credential design; per-tenant key vault; audit trail |
| **Counterfactual simulation pressure damages credibility** | Low | Medium | Replay > simulation, always; be loud about the honest-measurement framing |
| Solo / small team can't sustain pace | Medium | High | Phase milestones designed for 2–3 person team; reduce scope before stretching team |
| Funding runway misaligned with phase timing | Variable | High | Each phase has independent go/no-go; can pause without sunk-cost spiral |

---

## 9. Resource model

The roadmap assumes the following team shape; if reality differs,
phasing slows proportionally.

| Phase | Engineers | Other | Comment |
|---|---|---|---|
| Phase 0 | 1–2 | 0 | Founding engineer(s); product = personal use |
| Phase 1 | 2 | 0 | Same team; one focuses on docs/launch |
| Phase 2 | 2–3 | 0.5 (DR / design partner outreach) | Adapter breadth + contracts |
| Phase 3 | 3–4 | 1 (full-time DR / founder-led sales) | Cloud + Replay + lint + reconcile is the lift |
| Phase 4 | 5–7 | 2 (sales + marketing) | TypeScript + Library + GA |
| Phase 5 | 8–12 | 4+ (enterprise sales, SE, security) | Standard SaaS scale-up |

Solo-founder execution is *possible* through Phase 2 but slows the
phases by ~50%. A solo founder should be honest about whether they
can carry the project to Phase 3 without bringing on at least one
co-founder.

---

## 10. Comparison to alternatives we considered

| Alternative | Why we rejected it |
|---|---|
| Build a unified LLM gateway (LiteLLM-shaped) | Saturated market; well-funded incumbents; no defensible differentiation |
| Build our own agent framework (LangGraph competitor) | Adoption barrier too high; competes with funded incumbents directly |
| Build a request-level observability tool (Langfuse-shaped) | Langfuse and Helicone strong here; less defensible than agent-loop-aware |
| Build counterfactual simulation as the headline feature | Hard problem if attempted via modelling; results often inaccurate; erodes trust. Replay is the honest alternative |
| Be a pure OSS project with no SaaS plan | Doesn't fund itself; no career exit for founders |
| Be a pure SaaS with no OSS | No distribution wedge; in this category, OSS is the trust mechanism |
| Build aggregated 5-field token usage (NeutralUsage) | Commoditised; gives no defensible reason to choose Inkfoot over Langfuse |

The chosen approach — **causal attribution + contracts + replay +
static analysis + invoice reconciliation, OSS-instrumented and
optional SaaS** — is the only shape with both a low-barrier adoption
wedge and a defensible business model with multiple structural moats.
It mirrors the playbook that worked for Sentry, dbt Labs, Grafana
Labs, and LangChain Inc., with additional moats (Replay, static
analysis) that those incumbents didn't have at the equivalent stage.

---

## 11. Structural USPs and why they're hard to replicate

Three things in Inkfoot compound over time and are structurally
difficult for competitors to copy:

### USP 1 — Causal Token Ledger as the data substrate

The 13-category attribution is the foundation. Every other feature
sits on top of it. Competitors built on aggregated cost data
(Langfuse, Helicone) cannot retroactively add causal categories
without rebuilding their data model and reprocessing history.

**Defensibility duration:** ~6–12 months before competitors catch
up. Used to seed the other USPs.

### USP 2 — Cost Replay Engine

The ability to replay a recorded run with a different policy stack,
using recorded tool results as fixtures, produces real cost numbers
without modelled estimates. Competitors with summary-only event
storage cannot do this without rebuilding their data model.

**Defensibility duration:** ~18–36 months. The combination of "store
the right data" + "build the replay infrastructure" + "win the trust
to handle replay credentials" is a real architectural commitment.

### USP 3 — Static analysis + runtime instrumentation + replay (combined)

The position no current competitor occupies. Static-only tools don't
see runtime; runtime-only tools don't see code. Inkfoot sitting in
all three positions answers "what will this code change cost?" before
the code merges.

**Defensibility duration:** indefinite. To replicate this, a
competitor would need to build static analysis for multiple
frameworks + runtime instrumentation + replay infrastructure. That
is a 12–18 month catch-up effort even at well-funded pace, and by
then we have the Cost Smell Library and customer-attribution lock-in
on top.

These USPs are not standalone marketing claims — they're
**architectural decisions** baked into the Phase 0–3 design. The
roadmap exists to ship them.

---

## 12. Exit considerations

This roadmap is built to optimise for sustainable independent growth.
Honest acknowledgment of acquirer interest is part of strategic
planning. Likely acquirers, ranked by strategic fit:

1. **Datadog** — actively expanding into AI observability. Would buy
   Inkfoot for the agent-FinOps angle their request-level APM
   can't easily extend to. Realistic exit range at Phase 4
   maturity: **$25–60M**.
2. **Langfuse / Helicone** — direct adjacency. Would buy to absorb a
   credible competitor. Lower price band (team-acqui-hire shape).
3. **LangChain Inc.** — owns LangSmith. Would buy to extend their
   observability suite. Strategic fit good; price depends on
   funding posture.
4. **A FinOps incumbent (Apptio, Vantage, CloudHealth,
   Cloudability)** — wants an AI angle; cultural fit is harder.
5. **A hyperscaler (AWS, GCP, Azure)** — to bundle into LLM inference
   products. Lower probability; hyperscalers prefer to build.

The acquisition-shaped exit is real but not the goal. Building this
as a sustainable independent business *also* maximises acquisition
value; the two strategies do not diverge in execution. Optimise for
the business; let acquisition happen as a consequence.

Note: this exit estimate is higher than earlier drafts because the
strengthened USPs (Replay + static analysis + invoice reconciliation)
make Inkfoot harder to replicate, raising the price an acquirer
must pay rather than build.

---

## 13. Tracking

This roadmap is revisited at the end of each phase. Each phase end
produces:

- A retrospective document (what shipped, what we learned, what
  changed our mind).
- A go/no-go decision on the next phase.
- An updated version of this roadmap if any phase boundary moved.

The retrospectives go in `roadmap-retrospectives/`. Each is dated
and preserved; we do not edit the past.

---

## Open questions

- **Naming.** "Inkfoot" is the chosen product name; tagline is *"Find
  the hidden cost trail in every AI agent run."* Initial collision
  search across PyPI, npm, GitHub, and AI/software companies shows no
  conflicts in the product's market neighbourhood. Pre-launch work
  before Phase 1: USPTO TESS trademark filing in software classes 9
  and 42; secure `inkfoot.dev`, `inkfoot.ai`, and ideally `inkfoot.com`
  domains; reserve `inkfoot` on PyPI and `@inkfoot` scope on npm.
- **Open-governance posture.** Apache 2.0 from day one is the
  default. CLA requirements, governance committee, foundation
  membership questions deferred until Phase 4.
- **Open-data ethics.** Phase 4 introduces `inkfoot.dev/insights`
  and the Cost Smell Library verification corpus — anonymised data
  from real customers. Needs a written policy before Phase 3 about
  what we will and will not publish, and the opt-in mechanism.
- **The Sleuth relationship.** Inkfoot is technically separable
  from Sleuth but shares design heritage. Is Inkfoot a standalone
  company, a Sleuth feature, or an open-source project Sleuth (the
  company) maintains as a strategic asset? Implications differ for
  funding, branding, and exit. Defer until Phase 2 outcome is
  visible.
- **TypeScript port priority vs depth.** Phase 4 timing assumes
  Python validates the model first. If TS demand surfaces earlier,
  the port may move to Phase 3 (at the cost of slowing Cloud
  delivery).

---

## Assumptions

- A 2–3 person team is available from Phase 0; solo execution is
  possible but slows everything by ~50%.
- Pre-seed or seed funding ($500k–$2M) available by Phase 3 if
  needed. Phases 0–2 self-fundable from consulting or savings.
- The agent-framework ecosystem (LangGraph, OpenAI Agents SDK,
  Anthropic Agent SDK, Pydantic AI, CrewAI) remains roughly stable.
  Major fragmentation forces reshaping.
- LLM provider pricing remains the dominant cost driver for agent
  workloads. Severe inference-cost deflation would weaken the
  FinOps premise — but probably not eliminate it (relative cost
  attribution is still useful even when absolute cost is low).
- OpenTelemetry continues maturing its GenAI semantic conventions;
  our mapping work has a moving target but won't be invalidated.
- The current macro environment supports developer-tools SaaS
  fundraising. Severe market deterioration would extend timelines.
