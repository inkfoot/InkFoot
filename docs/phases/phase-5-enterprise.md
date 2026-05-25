# Phase 5 — Enterprise

**Theme:** *Become a credible enterprise procurement candidate.*
**Status:** approved scope; entered only after Phase 4 go-signal.
**Weeks:** 64+ (ongoing).
**Companion docs:**
- [Roadmap §7](../roadmap-inkfoot.md#7-phase-5--enterprise-weeks-64)
- [Architecture §10.3, ADR-007](../architecture-inkfoot.md)
- **External pattern reference:** Sleuth's `architecture-iam.md` —
  the IAM design Phase 5 inherits.

---

## 1. Outcome

Inkfoot is a credible enterprise procurement candidate. **SSO works
end-to-end** with Google, Azure Entra, and Okta. **Self-hosted
Cloud** ships, runnable in a customer VPC. **EU data residency**
goes live. **SOC 2 Type 2** is in progress or complete. We have at
least one Fortune-2000 logo *or* one $100k+ ARR contract.

The narrative shift: Phases 0–4 built a product the **individual
engineer** chose. Phase 5 builds the things the **procurement / IT
security / compliance / legal** desk requires before they let that
engineer's choice land on a corporate invoice.

This is the longest, most operational phase. Unlike Phases 0–4 it
has no hard end-date; it transitions into "normal SaaS scale-up
operations" once the procurement-readiness milestones are met.

## 2. What ships

| Deliverable | Architecture / pattern ref | Notes |
|---|---|---|
| **Full multi-tenant IAM** | Borrows Sleuth's `architecture-iam.md` | Tenants, memberships, identities, sessions tables |
| **SSO via OIDC** (Google, Azure Entra, Okta) | Sleuth IAM §6.1, §7.3 | Procurement-blocker until shipped |
| **SAML SSO** | Sleuth IAM §13 (forward-listed IE11) | Enterprise tier only; not Pro / Team |
| **RBAC** (Owner / Admin / Member / Viewer per workspace) | Sleuth IAM §8.1 | The four-role pattern; permissions in code |
| Audit log (Cloud-side) | Sleuth IAM §4.5, §5 | Compliance-grade; ≥ 1 year retention configurable |
| **SOC 2 Type 2** | — | Single biggest enterprise SaaS gate in this category |
| **Self-hosted Cloud distribution** | [Architecture §10.3](../architecture-inkfoot.md) | Docker Compose + Kubernetes Helm; runs in customer VPC; same code as managed |
| **EU data residency** | §10.2 | Frankfurt or Ireland region |
| **Postgres RLS** as defense-in-depth | Sleuth IAM ADR-007 | Application-code scoping + database-enforced isolation |
| Dedicated solutions engineer playbook | — | Onboarding doc, pricing model, sample contracts |
| Annual contract billing flow | — | Invoiced; not Stripe-self-serve |
| SCIM provisioning | Sleuth IAM §13 forward-list (IE14) | Larger customers' provisioning need |
| Per-tenant data export | — | "Give me everything you have on us" — GDPR-aligned |
| HIPAA BAA (on request) | — | Healthcare-vertical customers |

## 3. What we deliberately do NOT build

- Anything not yet asked for by an actual paying enterprise prospect.
  Phase 5 is **demand-driven**, not feature-driven. The list above is
  the *most-asked* procurement requirements; subsequent additions
  follow specific customer asks with revenue commitment.
- A separate "Enterprise" product fork. Same codebase; Enterprise
  features are flag-gated.
- Public roadmap for Enterprise features. Account managers carry
  the roadmap to specific customers.

## 4. Architecture this phase exercises

The architecture this phase **mostly does not change**. Phase 5 is
about adding the operational and security surface around the
product Phases 0–4 already built:

- **§9.3 Privacy** — formalised under SOC 2 controls.
- **§10.2 Cloud deployment** — adds EU region.
- **§10.3 Self-hosted Cloud** — fully realised (the architecture
  already declared this as the shape; Phase 5 actually ships it).
- **§4.14 Cloud backend** — adds the IAM tables (tenants,
  memberships, identities, sessions, audit_events,
  personal_access_tokens, mfa_factors, sso_providers); ships the
  full IAM stack from Sleuth's design as a single migration.

The architecture's **ADR-007** (API key auth in Cloud v1; full IAM
in Phase 5) is realised in this phase. The transition from API-key
auth to IAM-and-API-keys-both is a load-bearing migration.

## 5. Definition of done

Phase 5 has multiple acceptance milestones, not a single "done":

### Procurement readiness

- [ ] SSO works end-to-end with Google, Azure Entra, Okta (OIDC).
- [ ] SAML SSO works with at least one major IdP integration tested
      against a representative customer's IdP.
- [ ] RBAC enforced; the four-role split actually works for two
      enterprise customers in production.
- [ ] Audit log complete; ≥ 1 year retention; exportable.

### Operational readiness

- [ ] Self-hosted Cloud shipped to ≥ 2 customers in production.
- [ ] EU region live; data residency selectable at workspace
      creation; cross-region transfer prohibited at the application
      layer (and via Postgres RLS).
- [ ] SOC 2 Type 2 audit passed.
- [ ] HIPAA BAA template signed with at least one healthcare-
      adjacent customer (if applicable to the customer base).

### Business readiness

- [ ] ≥ 1 customer paying ≥ $100k/year, OR three customers paying ≥
      $30k/year.
- [ ] Annual contract billing flow tested end-to-end (procurement →
      contract → invoice → revenue recognition).
- [ ] Dedicated solutions engineer playbook documented; first
      enterprise customer onboarded against it.
- [ ] Per-tenant data export ("give me everything") works
      end-to-end; tested against the largest tenant's data.

## 6. Go/no-go signal — Phase 5 and beyond

By Phase 5, the company-or-acqui-hire conversation is the focus,
not go/no-go. Continued investment is justified by **standard SaaS
scale-up signals**:

- Net revenue retention (NRR ≥ 110% is healthy).
- Sales efficiency (LTV / CAC).
- Magic number (new ARR per dollar spent on S&M).

**If those numbers are healthy:** raise a Series A. The acquirer
landscape from roadmap §12 ranks Datadog first, Langfuse / Helicone
second, LangChain Inc. third. Continued independence and
acquisition aren't divergent strategies in execution; optimise for
the business and let acquisition happen as a consequence.

**If those numbers are flat:** optimise for profitability. Phase 5
features (SSO, self-hosted, SOC 2) make Inkfoot acquisition-
attractive whether or not we keep growing into a standalone
business; this is a healthy off-ramp.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **SOC 2 audit timing slips.** First Type 2 audit period is ~6 months; auditor backlog can extend that. | Engage auditor at Phase 5 week 0; pre-audit readiness against a published controls catalogue (SOC 2 CC + applicable trust services criteria); document evidence collection from Phase 3 forward (audit-ready from earliest Cloud day). |
| **Self-hosted Cloud diverges from managed Cloud.** Two code paths quietly drift; bugs that affect only self-host customers become unreproducible internally. | Same codebase, flag-gated; CI runs the test suite against both deploy shapes; quarterly internal self-host install / smoke test as a release gate. |
| **EU region adds operational complexity (latency, cross-region replication, GDPR).** | EU region is independently deployed (no cross-region data flow); customer chooses at workspace creation; no migration path between regions (a wider design choice; customers re-create workspaces if they want to switch). |
| **SSO integration edge cases.** Each IdP has quirks (Azure Entra group claims, Okta's app-launch flow). | Start with Google (cleanest); validate Azure Entra and Okta against a customer's actual IdP before broader launch. Document quirks publicly to attract integration partners. |
| **Pricing power vs procurement.** Enterprise procurement negotiates down by default. Without anchoring on value, ASP collapses. | Tie Enterprise pricing to invoice-reconciled savings: "you saved $X/month per the reconciliation report; price is Y% of that." The savings number is Inkfoot's own data telling the customer's CFO what they paid for. |
| **HIPAA BAA scope.** Healthcare-adjacent customers want it; the actual scope of "we never touch PHI" depends on the privacy posture (metadata-only by default). | Privacy posture from architecture §9.3 makes BAA scope narrow; have the legal review pre-Phase-5; sign templates per-customer as asked. |
| **The product becomes operationally heavy.** Enterprise customers have support burden; the team becomes 50% ops. | Phase 5 budgets dedicated CS / SE roles from the start; founders are not the post-sales contact. |

## 8. Suggested epic breakdown (for later)

Prefix **EE** (Enterprise Epic — distinct from `EX` for Explain).
Suggested:

- **EE1** — IAM schema landed in Cloud (mirrors Sleuth's IE1):
  tenants + memberships + identities + sessions + audit_events.
- **EE2** — OIDC SSO (Google + Azure Entra + Okta) — Cloud-side
  provider registry + callback handling + identity-linking flow
  (Sleuth IAM ADR-003: auto-link only on verified email).
- **EE3** — RBAC enforcement (the `RequireRole` no-op stub from
  Phase 4 becomes real enforcement; four roles; permissions table).
- **EE4** — Audit log (Cloud-side writer + query API + export).
- **EE5** — SAML SSO + SCIM provisioning.
- **EE6** — Self-hosted Cloud distribution (Docker Compose + Helm;
  feature parity gate; release process).
- **EE7** — EU region (separate deployment; data-residency
  enforcement; tenant-creation UX).
- **EE8** — Postgres RLS (per-tenant policies; defense-in-depth).
- **EE9** — SOC 2 Type 2 (controls catalogue; evidence collection
  pipeline; auditor engagement; remediation).
- **EE10** — Annual / invoiced billing flow (procurement-friendly;
  not Stripe-self-serve).
- **EE11** — Per-tenant data export ("GDPR-aligned").
- **EE12** — HIPAA BAA template + scoping doc.
- **EE13** — Dedicated solutions engineer playbook (onboarding,
  pricing, contract templates).
- **EE14** — Enterprise contract reference customers (the published
  case studies that unlock subsequent procurement conversations).

EE1 + EE2 + EE3 + EE4 are the IAM core. EE6 + EE9 are the most
operationally heavy. EE10 + EE13 are commercial enablers, not
engineering deliverables.

## 9. Open questions

- **Acquirer conversations.** Phase 5 milestones materially raise
  the acquisition price (see roadmap §12). Pre-Phase-5 decision:
  do we *engage* acquirers proactively, or only respond to inbound?
  Default: inbound-only; do not signal intent to sell during Phase
  5; let the SaaS metrics speak.
- **Self-hosted Cloud licensing.** OSS code is Apache 2.0 (decided
  Phase 1). Self-hosted Cloud is the SaaS code running in customer
  VPC — is that Apache 2.0 too, or source-available (ELv2 / BUSL)?
  Default: source-available for the SaaS code; Apache 2.0 for the
  OSS library remains unchanged. This is a load-bearing decision
  that touches OSS messaging; revisit with legal counsel pre-Phase
  5.
- **HIPAA scope creep.** Healthcare BAA may pull us into PHI-
  adjacent features customers don't yet ask for. Default: BAA
  scope is narrow (metadata only); refuse PHI-bearing feature
  requests unless commercial commitment is large enough to justify
  the compliance investment.
- **Per-customer feature flags.** Enterprise customers will ask for
  specific behavioural toggles. How permissive are we? Default:
  policy-driven (per-tenant settings rows) is acceptable; per-
  customer code branches are not.
- **EU region selection.** Frankfurt vs Ireland vs both. Default:
  Frankfurt first (broader EU regulatory coverage); Ireland as a
  follow-up.
- **The Sleuth relationship at Phase 5 scale.** Roadmap §
  open-questions: "Is Inkfoot a standalone company, a Sleuth
  feature, or an open-source project Sleuth maintains as a
  strategic asset?" This *must* be resolved before Phase 5
  enterprise contracts; the answer affects funding, branding, and
  who signs the contract. Decision point: Phase 4 exit.
