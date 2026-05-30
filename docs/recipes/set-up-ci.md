# Recipe: Set up CI cost review

Wire `inkfoot benchmark` + `inkfoot diff` into your pull-request
workflow so prompt changes that double the bill show up as a PR
comment, not as a surprise on next month's invoice. Target: under
ten minutes.

## What you'll need

- A repository on GitHub. (GitLab and Bitbucket variants live in
  [recipes/ci-gitlab](ci-gitlab.md) and
  [recipes/ci-bitbucket](ci-bitbucket.md).)
- A directory of *scenarios* — `.py` files describing a unit of
  agent work the CI run should re-execute. Scenarios are tiny;
  three to five per task is plenty.
- A provider API key in GitHub Actions secrets
  (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`). Benchmark runs make
  real LLM calls — that's the point — so use a test-account key
  with a spend cap.

## 1. Write a scenario

A scenario is a Python file with two exports: `INKFOOT_SCENARIO`
(a dict describing the unit of work) and `run(fixture)` (a
callable Inkfoot will invoke).

```python title="tests/agent_scenarios/customer_support_triage.py"
INKFOOT_SCENARIO = {
    "task": "customer-support-triage",
    "fixtures": ["fixtures/ticket-1.json", "fixtures/ticket-2.json"],
    "expected_outcome": "success",
    "runs_per_fixture": 1,
}


def run(fixture: dict) -> dict:
    # Import your real agent and call it with the fixture. Anything
    # Inkfoot-instrumented (the SDK shim, a framework adapter, or
    # @agent_run) records what happens here.
    from myapp.agents import triage

    return triage.handle(fixture)
```

Each fixture is a JSON file under your scenarios directory.
Inkfoot reads it as a dict and hands it to `run()`.

## 2. Generate a baseline

Run the benchmark once on `main` and commit the artefact so the
CI run has something to diff against.

```bash
inkfoot benchmark ./tests/agent_scenarios --output baseline.json
git add baseline.json && git commit -m "Add Inkfoot benchmark baseline"
```

The JSON shape is documented in
[`inkfoot benchmark`](../reference/cli.md#inkfoot-benchmark);
it's schema-versioned so future Inkfoot releases can read your
baseline.

## 3. Add the GitHub Action

```yaml title=".github/workflows/cost-review.yml"
on:
  pull_request:
    paths:
      - "src/agents/**"
      - "tests/agent_scenarios/**"

jobs:
  cost:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: inkfoot/diff-action@v1
        with:
          scenarios: ./tests/agent_scenarios
          baseline-source: path:baseline.json
          fail-threshold: default
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

What the action does, in order:

1. Installs `inkfoot[all]` on the runner.
2. Resolves the baseline (here: from a committed `baseline.json`
   in the repo; `artifact:<workflow>:<name>` and
   `release:<tag>:<asset>` are the other modes).
3. Runs `inkfoot benchmark ./tests/agent_scenarios --output current.json`.
4. Runs `inkfoot diff baseline.json current.json --format markdown`.
5. Posts a sticky PR comment with the markdown.
6. Uploads `current.json` as a build artefact.
7. Exits with `0` (ok), `1` (warn), or `2` (fail) per the
   [diff threshold table](../reference/cli.md#inkfoot-diff).

## 4. Trigger a regression to see the comment

Edit a system prompt in your agent code, raise the temperature,
or otherwise tickle a cost difference, and open a pull request.
The Inkfoot cost-review job runs, the action posts:

```markdown
## Inkfoot cost diff · ⚠️ warn

_Thresholds preset: **default** · baseline 2026-05-25T12:00:00Z → current 2026-05-29T17:42:00Z_

| Scenario | p50 Δ | p95 Δ | cache hit Δ | LLM calls Δ | Verdict |
|---|---|---|---|---|---|
| customer-support-triage | +32.1% | +28.4% | -2.3pp | +0.40 | ⚠️ warn |

### Regressions

- **customer-support-triage** — ⚠️ warn
  - p50 cost regressed by 32.1% (warn threshold +20.0%)
```

The thresholds are configurable per workflow run. The defaults
(documented in the [CLI reference](../reference/cli.md#inkfoot-diff))
warn at +20% / fail at +50% on cost and warn at -10pp / fail at
-25pp on cache hit rate.

## 5. Roll the baseline forward

After the PR merges and your `main` branch reflects the new
"normal" cost level, regenerate the baseline. The simplest path:

```bash
git checkout main && git pull
inkfoot benchmark ./tests/agent_scenarios --output baseline.json
git add baseline.json && git commit -m "Bump Inkfoot baseline"
```

Or, for a no-touch flow, point `baseline-source` at the
artefact produced by the prior main-branch run:

```yaml
with:
  scenarios: ./tests/agent_scenarios
  baseline-source: artifact:cost-review.yml:inkfoot-benchmark-current
  fail-threshold: default
```

The action will `gh run download` the most recent successful
main-branch run's `inkfoot-benchmark-current` artefact every
time it executes.

## Other CI systems

Same flow without the GitHub Action wrapper:

- [CI on GitLab](ci-gitlab.md)
- [CI on Bitbucket](ci-bitbucket.md)

## Next step

The action's sticky comment includes a `Smell changes` section
that tells you *why* the cost moved. If a new smell appears in a
PR diff and you don't know what it means:

→ [Cost Smells](../concepts/cost-smells.md) — the catalogue.
