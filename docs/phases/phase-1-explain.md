# Phase 1 — Explain

**Theme:** *Explain why every token was spent. Ship to the world.*
**Status:** approved scope; entered only after Phase 0 go-signal.
**Weeks:** 8–20 (12 weeks).
**Companion docs:**
- [Roadmap §3](../roadmap-inkfoot.md#3-phase-1--explain-weeks-820)
- [Architecture §4.1, §4.4, §4.8, §4.11, §4.12](../architecture-inkfoot.md)

---

## 1. Outcome

Inkfoot is **publicly released on PyPI** with a credible README, a
launch blog post, framework adapters for the dominant agent
frameworks, an OpenTelemetry-compatible event surface, and the
**`inkfoot diff`** CI-cost-review workflow. We are present in the
conversation about agent cost.

The wall-time deliverable: the library is installable by anyone, the
docs explain what it does, and at least three external users have
real runs recorded for more than seven days after install.

The narrative shift from Phase 0 to Phase 1: in Phase 0 we *found*
issues in our own data. In Phase 1 we *tell that story publicly* —
the launch blog post is literally "we measured our own agents and
learned X," with real numbers.

## 2. What ships

| Deliverable | Architecture ref | Notes |
|---|---|---|
| Framework adapter for **LangGraph** | [§4.1](../architecture-inkfoot.md) | Pattern C; largest agent-framework share mid-2026 |
| Framework adapter for **OpenAI Agents SDK** | §4.1 | Pattern C; OpenAI-bound, big and growing |
| Framework adapter for **Anthropic Agent SDK** | §4.1 | Pattern C; smaller but high-quality usage |
| Framework adapter for **raw `anthropic` / `openai` SDKs** | §4.1 | Pattern B (decorator) + run-scoping |
| **`inkfoot benchmark`** + **`inkfoot diff`** | §4.8 | CI cost review; structured JSON + human-readable PR-comment output |
| **GitHub Action** wrapper `inkfoot/diff-action` | §4.8 | One-line CI integration |
| **OpenTelemetry ingest** (`POST /api/v1/otel` shape; locally accepted via `inkfoot.otel_ingest()` helper) | §4.11, ADR-012 | GenAI semantic conventions mapped |
| **OpenTelemetry export** to any OTel collector | §4.11 | Datadog / Honeycomb / Grafana etc. backends |
| Recommendation engine surfaces inline in `inkfoot report` | §4.4 | The smells already exist (Phase 0); now they appear inline rather than only on demand |
| Public docs site (`inkfoot.dev` or equivalent) | — | Searchable; quickstart; recipes; API reference |
| Three published recipes | — | "Find your most expensive agent", "Spot cache-miss patterns", "Set up CI cost review" |
| Launch blog post | — | "We measured our own agents and learned X" |
| CI matrix across Python 3.10/3.11/3.12 | — | OSS hygiene |
| Apache 2.0 LICENSE file | — | Final license decision documented per ADR (separate small doc) |
| Contribution guide + CoC + issue templates | — | OSS hygiene |
| Public GitHub mirror | — | The OSS-trust surface |

## 3. What we deliberately do NOT build

- **Cloud infrastructure** — Phase 3.
- **Token Contracts** — Phase 2.
- **Modification policies** (`LazyToolExposure`, `CheapSummariser`) —
  Phase 2.
- **Cost Replay Engine** — Phase 3.
- **Static analyzer** — Phase 3.
- **Invoice reconciliation** — Phase 3.
- **TypeScript port** — Phase 4.
- **Pydantic AI / CrewAI adapters** — Phase 2.
- **Gemini / Bedrock / OpenAI-compat providers** — Phase 2.
- **Cost-per-success as headline metric** — Phase 2 (the tag is
  already captured in Phase 0; promotion happens with Phase 2's
  reporting work).

## 4. Architecture this phase exercises

Newly implemented vs Phase 0:

- **§4.1** — Pattern B (decorator) + Pattern C (framework adapters) on
  top of the Pattern A foundation from Phase 0.
- **§4.4** — Reports now surface smells inline (engine itself is
  Phase 0).
- **§4.8** — `inkfoot diff` and `inkfoot benchmark`.
- **§4.11** — OTel ingest + export.
- **§4.12** — `inkfoot report` gains a smell-inline rendering; new
  CLI commands (`benchmark`, `diff`).

Still **not** exercised:

- §4.6 Token Contracts (Phase 2).
- §4.9 Cost Replay Engine (Phase 3).
- §4.10 Static analyzer (Phase 3).
- §4.13–§4.16 Cloud surfaces (Phase 3+).

## 5. Definition of done

- [ ] `pip install inkfoot` from **public** PyPI.
- [ ] Docs site live; quickstart works for a first-time user without
      help.
- [ ] Launch blog post published on a credible engineering blog (our
      own or a partner's).
- [ ] OTel ingest validated against a reference OpenTelemetry
      collector + at least one external backend (Honeycomb or Grafana
      Tempo).
- [ ] At least one significant external reach event: HN front-page
      submission, LangChain official roundup mention, Anthropic blog
      signal, or a PyCon / AI-conference talk acceptance.
- [ ] Public GitHub mirror with Apache 2.0 license + contribution
      guide.
- [ ] **Three external users** outside our team with runs recorded
      > 7 days after install.
- [ ] `inkfoot benchmark` + `inkfoot diff` integrated as a CI gate
      in this repo and one external repo as a reference.
- [ ] GitHub Action `inkfoot/diff-action` published with an
      end-to-end test against a sample agent repo.

## 6. Go/no-go signal — Phase 1 → Phase 2

Phase 1 transitions to Phase 2 (Enforce) **if at the 8-week mark
post-launch** we have **one** of:

- ≥ 500 GitHub stars, OR
- ≥ 100 unique PyPI installs/day, OR
- ≥ 5 external contributors with real issue threads.

**Hitting one is the bar.** Hitting two is healthy. Hitting none
means the wedge isn't landing — reshape positioning, messaging, or
the entry point before continuing.

The honest framing: this is the moment we learn whether agent
engineers care enough about cost-attribution-as-a-product to invest
even the 15-second cost of `pip install inkfoot`. The OSS launch is
the cheapest version of that question we can ask.

## 7. Phase-specific risks

| Risk | Mitigation |
|---|---|
| **Launch doesn't get traction.** Multiple parallel launches misfire; the wedge isn't landing. | Pre-line up 4+ launch channels (blog, HN, framework communities, Reddit r/MachineLearning, AI conference talk). If first wave is flat, pivot positioning before second wave; don't burn all channels on the same message. |
| **Framework adapter scope creep.** Each adapter is "small" but four × small adds up; quality matters more than coverage. | Pick the two most-used adapters first (LangGraph + OpenAI Agents SDK); ship those at quality; treat Anthropic Agent SDK + raw-SDK as the second wave inside Phase 1. |
| **OTel mapping drift.** The GenAI semantic conventions are evolving fast in 2026. Mappings will need adjustment. | Version mapping logic; test against upstream conformance suites; the `inkfoot.*` attribute namespace gives us room to extend without breaking. |
| **CI integration breaks.** GitHub Action / Bitbucket Pipelines / GitLab CI behave differently; one breaks before launch. | Pre-test on each major CI; ship GitHub Action first as the canonical reference; others are best-effort. |
| **An incumbent ships causal attribution before us.** Langfuse / Helicone announces 13-category attribution mid-Phase-1. | Speed; lean into the architectural USPs (Replay + Token Contracts + static analysis) coming in Phases 2–3 as the harder-to-copy combination; emphasise "we did this in our own production agents for six weeks before launching" as the trust signal. |
| **Docs site is forgettable.** Quickstart works but the "why" isn't compelling enough; readers bounce. | The launch blog post is the docs site's headline page on launch day; treat the blog as a permanent doc, not a one-time post. |

## 8. Suggested epic breakdown (for later)

Prefix **EX** (Explain). Suggested:

- **EX1** — LangGraph framework adapter (Pattern C; the largest
  market-share single delivery).
- **EX2** — OpenAI Agents SDK framework adapter.
- **EX3** — Anthropic Agent SDK framework adapter.
- **EX4** — Raw SDK Pattern B + run-scoping (`@agent_run` /
  `with inkfoot.agent_run(...):`).
- **EX5** — `inkfoot benchmark` (scenario runner; deterministic-as-
  possible execution; JSON output).
- **EX6** — `inkfoot diff` (compare benchmark JSON; PR-comment
  formatting; exit-code contract).
- **EX7** — GitHub Action `inkfoot/diff-action` (publish to
  marketplace; end-to-end test in a sample repo).
- **EX8** — OTel ingest mapping (GenAI semantic conventions →
  `NeutralCall` + ledger).
- **EX9** — OTel export shim (Inkfoot events → OTel-compatible
  emission).
- **EX10** — Docs site (`inkfoot.dev`): quickstart, three recipes,
  API reference, OTel mapping reference.
- **EX11** — Launch (blog post, HN submission, framework community
  outreach, conference CFP).
- **EX12** — Public OSS hygiene (LICENSE, CoC, contribution guide,
  issue templates, CI matrix across Python 3.10/3.11/3.12).
- **EX13** — Adoption telemetry (privacy-preserving "did install
  succeed" pings; opt-in; the only telemetry the OSS does).

EX1 + EX2 + EX5 + EX6 + EX10 + EX11 are the load-bearing minimum
slice. EX7 (GitHub Action) is the highest-leverage CI piece; ship
inside Phase 1 even if EX3 + EX4 slip into a follow-up.

## 9. Open questions

- **Which CI ecosystem gets the first-class action?** GitHub Action
  is obvious; the Bitbucket / GitLab equivalents matter for
  enterprises. Phase 1: GitHub first-class; others "use the CLI
  directly with these copy-paste snippets."
- **Should the docs site be statically generated or dynamic?** The
  contents barely changes between releases. Default: static
  (mkdocs-material or similar) for forever-cheap hosting; revisit
  if interactive examples land.
- **Privacy posture for adoption telemetry.** Opt-in or opt-out?
  Default: opt-in (matches our metadata-only-by-default privacy
  posture in §9.3 of the architecture). Cost: less data on adoption
  trajectory.
- **Apache 2.0 vs MIT vs ELv2 / BUSL.** Default: Apache 2.0. The
  defensibility argument for source-available is weak when the data
  itself (Causal Token Ledger corpus + verified smells) is the moat,
  not the code.
