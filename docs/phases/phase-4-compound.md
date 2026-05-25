# Phase 4 — Compound

**Theme:** *Make the moats compound. TypeScript port, community Smell Library, Cloud GA.*
**Status:** approved scope; entered only after Phase 3 go-signal.
**Weeks:** 48–64 (16 weeks).
**Companion docs:**
- [Roadmap §6](../roadmap-inkfoot.md#6-phase-4--compound-weeks-4864)
- [Architecture §4.16](../architecture-inkfoot.md)

---

## 1. Outcome

Cloud is **publicly available** with self-serve signup. The **Cost
Smell Library** opens to community contribution with verified
savings data — the OSS-mindshare moat begins to compound. The
**TypeScript port** lands, opening the JS/TS agent market. We have
**15–30 paying customers** and a clear story for what's next.

The narrative shift: Phase 3 proved the product to design partners.
Phase 4 **scales** beyond them — self-serve signup, community-
contributed rules, a second language ecosystem. This is the phase
where Inkfoot becomes a product, not a project.

## 2. What ships

| Deliverable | Architecture ref | Notes |
|---|---|---|
| **Cost Smell Library** (`library.inkfoot.dev`) | [§4.16](../architecture-inkfoot.md), ADR-011 | Community-contributed smell definitions with verification data |
| Smell contribution workflow | §4.16 | PR-based; core-team review; verification against opted-in customer corpus |
| Private smells | §4.16 | Customers can author internal-only smells for their workspaces |
| **TypeScript port** (`@inkfoot/sdk` on npm) | §10.1, §15 open questions | Pattern A (SDK shim) + Pattern B (decorator) + Pattern C adapters for Vercel AI SDK, OpenAI Node SDK, Anthropic TS |
| Cloud public signup | — | Self-serve, Stripe billing, no sales call for Pro |
| **Anomaly-based alerting** ("3σ deviation from baseline") | §4.14 | Complements Phase 3's threshold-based alerts |
| **Slack + PagerDuty integrations** for alert delivery | §4.14 | Most-asked integrations from design-partner conversations |
| Cloud cost attribution v2 | §4.14 | Per-customer-attribute rollups, cohort analysis, percentile breakdowns |
| Public status page | — | Standard SaaS hygiene |
| Public roadmap site | — | OSS contributors + Cloud customers see and vote on what's next |
| Inbound marketing blog | — | 5 published posts on FinOps patterns |
| `inkfoot.dev/insights` | §9.3, roadmap §6 | Anonymised cost-smell case studies (with permission); the "consent and publication policy" referenced as a Phase-3 open question is finalised here |
| Invoice reconciliation extended to **AWS Bedrock + Gemini** | §4.15 | Phase 3 covered Anthropic + OpenAI; Phase 4 extends |
| Static analyzer extended to **TypeScript** | §4.10 | TS lint rules; LangChain.js + Vercel AI SDK code patterns |

## 3. What we deliberately do NOT build

- **Full multi-tenant IAM, SSO, SAML, RBAC** — Phase 5.
- **SOC 2 Type 2 audit** — Phase 5.
- **Self-hosted Cloud distribution** — Phase 5.
- **EU data residency** — Phase 5.
- **Annual / invoiced billing flow** — Phase 5 (Stripe self-serve
  continues through Phase 4).
- **Postgres RLS** — Phase 5 (defense-in-depth on tenant isolation).
- **Enterprise solutions-engineer playbook** — Phase 5.

## 4. Architecture this phase exercises

Newly implemented vs Phase 3:

- **§4.16 Cost Smell Library** — the contribution workflow, the
  verification mechanism (the data moat), `library.inkfoot.dev`
  distribution.
- **§4.10 Static analyzer** — extended to TypeScript agent
  frameworks (Vercel AI SDK, LangChain.js).
- **§4.14 Cloud backend** — anomaly-based alerting; Slack + PagerDuty
  delivery integrations; v2 attribution rollups.
- **§4.15 Invoice reconciliation** — Bedrock + Gemini extensions.

The TypeScript port is materially new: Pattern A/B/C SDK shims
designed mirror-image to the Python library; the neutral event
shape carries over verbatim; same Cloud ingestion endpoint accepts
both.

## 5. Definition of done

- [ ] TypeScript SDK on npm; parity with Python's Pattern A + B.
- [ ] At least two TypeScript framework adapters (Vercel AI SDK +
      one other).
- [ ] Cost Smell Library has **≥ 20 community-contributed smells**
      with verification data.
- [ ] **≥ 15 paying customers** across Pro / Team tiers.
- [ ] **$20k+ MRR.**
- [ ] Inkfoot cited in at least one external article or conference
      talk we did not write or organise.
- [ ] OSS adoption ≥ 200 weekly active installs.
- [ ] Anomaly-based alerting fires correctly on a synthetic
      time-series fixture; false-positive rate < 5% in design-
      partner usage.
- [ ] Invoice reconciliation for Bedrock + Gemini matches the
      quality of the Phase-3 Anthropic + OpenAI implementations.
- [ ] Public status page live; uptime over Phase 4 ≥ 99.9%.

## 6. Go/no-go signal — Phase 4 → Phase 5

Phase 4 transitions to Phase 5 (Enterprise) if all of:

- MRR growing month-over-month at ≥ 15%, AND
- ≥ 1 customer at Team tier (≥ $249/mo) for ≥ 3 consecutive months,
  AND
- Sales conversations include enterprise-adjacent companies (Series
  B+ with > 30 engineers, or Fortune-2000 lines of business).

**If growth is flat:** stay self-serve. Double down on product —
more adapters, deeper Smell Library, better dashboards. Phase 5's
enterprise investment doesn't pay off without growth velocity.

**If growth is strong and enterprises are knocking:** Phase 5 is
justified. SSO, self-hosted, SOC 2 are the procurement-unlock
sequence.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **Cost Smell Library coordination cost.** Community contribution requires review, verification, curation. Without dedicated effort the library either stays small or admits low-quality smells. | Budget a **part-time community manager** from Phase 4 onward. Pre-define a smell contribution standard (Open question from Phase 3); ship the rubric in the launch announcement; reject low-quality contributions in week 1 to set the bar. |
| **TypeScript port priority vs depth.** TS agent ecosystem is real but smaller than Python. Splitting effort risks neither shipping well. | Phase 4 timing already gates this: Python validates in Phase 3 before TS work begins. Pattern A + B + 2 framework adapters is the launch slice; depth comes in Phase 5+. |
| **`inkfoot.dev/insights` opens a privacy attack surface.** Anonymised case studies could de-anonymise via correlation. | Pre-Phase-3 consent and publication policy (was an open question in Phase 3; *must* be finalised before Phase 4 ships any post). k-anonymity floor; per-customer opt-in for each post. |
| **Anomaly-based alerting false-positive flood.** 3σ thresholds on noisy distributions surface a lot of false alerts. | Per-tenant tuning; "noisy" alerts auto-suppress; alert quality is a published metric (customer-visible). |
| **Self-serve signup invites abuse.** Free-tier abuse from automated agents instrumenting throw-away workspaces. | Email-verified signup; per-IP signup rate limits; soft delete of inactive free workspaces after 90 days; the architecture's privacy posture (no content uploaded by default) limits the abuse surface anyway. |
| **An incumbent (Langfuse / Helicone / LangSmith) launches a competing Smell Library.** | The verification data is the moat, not the rule set. Anyone can copy a rule; only Inkfoot has the run corpus to verify the rule's impact. Lean into "verified savings" as the headline term. |

## 8. Suggested epic breakdown (for later)

Prefix **CO** (Compound). Suggested:

- **CO1** — TypeScript SDK Pattern A (SDK shim for `openai` /
  `@anthropic-ai/sdk` / `@google/generative-ai`).
- **CO2** — TypeScript SDK Pattern B (decorator + run-scoping; React
  / Vercel idioms).
- **CO3** — TypeScript Vercel AI SDK adapter (Pattern C).
- **CO4** — TypeScript framework adapter #2 (LangChain.js or
  Mastra; design-partner-driven selection).
- **CO5** — **Cost Smell Library** distribution
  (`library.inkfoot.dev`; Cloud auto-pull on startup; built-in vs
  community separation).
- **CO6** — Smell contribution workflow (GitHub-PR-based; review
  checklist; verification gate against opted-in customer corpus).
- **CO7** — Private smells (per-workspace authoring; share with
  workspace members; never published to library).
- **CO8** — Anomaly-based alerting (per-tenant baseline learning;
  3σ surface; suppression for known-noisy tasks).
- **CO9** — Slack + PagerDuty alert delivery (per-tenant channel
  config; webhook hygiene).
- **CO10** — Cost attribution v2 (per-tag rollups; cohort analysis;
  percentile breakdowns; saved views).
- **CO11** — Invoice reconciliation for AWS Bedrock (Cost Explorer
  by-tag).
- **CO12** — Invoice reconciliation for Google Gemini (Cloud
  Billing by-project).
- **CO13** — Static analyzer for TypeScript (8 rules mirroring
  Phase-3 Python rules; LangChain.js + Vercel AI SDK code
  patterns).
- **CO14** — Public signup flow (Stripe self-serve from $0 → Pro →
  Team).
- **CO15** — Public status page (third-party hosted; standard SaaS
  hygiene).
- **CO16** — Public roadmap site (votable; OSS + Cloud user
  visibility).
- **CO17** — `inkfoot.dev/insights` (anonymised case studies;
  consent policy enforced; k-anonymity floor).
- **CO18** — Inbound marketing blog (5 posts; FinOps patterns;
  written by the team).

CO1 + CO2 + CO3 + CO5 + CO6 + CO14 are the load-bearing minimum.
CO13 (TS static analyzer) is the highest-leverage breadth play;
delays here are acceptable if rule quality is at stake.

## 9. Open questions

- **TypeScript ecosystem coverage.** LangChain.js vs Mastra vs
  Vercel AI SDK — pick which gets the first-class second adapter.
  Default: Vercel AI SDK first (biggest mid-2026 mind share in TS);
  LangChain.js second; Mastra evaluated based on Phase-3 design-
  partner asks.
- **Smell Library governance.** Foundation-style governance (Apache,
  Linux, OpenJS) deferred to Phase 4 — the question is whether to
  defer further or commit now. Default: defer; ship a "core team
  reviews" model in Phase 4; revisit foundation in Phase 5.
- **Cost Smell Library license.** Apache 2.0 for the rule
  definitions; verification data not redistributed. Confirm this
  ahead of community launch.
- **`inkfoot.dev/insights` post cadence.** Monthly or as-it-happens?
  Default: monthly. Burns less editorial cost; less attack surface
  for de-anonymisation correlation.
- **Public roadmap vote weighting.** All votes equal vs Cloud-
  customer-weighted vs paying-customer-weighted. Default: all equal;
  surface the breakdown so contributors and customers see the
  pattern; never hide it.
