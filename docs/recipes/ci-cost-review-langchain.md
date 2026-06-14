# Recipe: Wire CI cost review for a LangChain repo

Catch the prompt change that doubles your LangChain agent's bill on
the pull request that introduces it — not on next month's invoice.
This recipe wires `inkfoot benchmark` + `inkfoot diff` into GitHub
Actions for a LangChain/LangGraph codebase. Target: under ten minutes.

## What you'll need

- A repository on GitHub with a LangChain or LangGraph agent.
  (GitLab and Bitbucket variants live in
  [recipes/ci-gitlab](ci-gitlab.md) and
  [recipes/ci-bitbucket](ci-bitbucket.md).)
- A directory of *scenarios* — `.py` files that re-run a unit of agent
  work. Three to five per task is plenty.
- A provider API key in GitHub Actions secrets (`ANTHROPIC_API_KEY` or
  `OPENAI_API_KEY`). Benchmark runs make real LLM calls — that's the
  point — so use a test-account key with a spend cap.

## 1. Write a scenario that calls your LangChain agent

A scenario exports `INKFOOT_SCENARIO` (a dict describing the work) and
`run(fixture)` (a callable Inkfoot invokes). For a LangChain repo,
`run` just calls your real agent — the handler captures every model
call inside it, no extra instrumentation in the scenario.

```python title="tests/agent_scenarios/rag_qa.py"
INKFOOT_SCENARIO = {
    "task": "rag-qa",
    "fixtures": ["fixtures/question-1.json", "fixtures/question-2.json"],
    "expected_outcome": "success",
    "runs_per_fixture": 1,
}


def run(fixture: dict) -> dict:
    # Your real LangGraph/LangChain agent. inkfoot.instrument() in its
    # startup path means every chat-model call here is recorded; if it
    # uses LangGraph, the per-node attribution comes along for free.
    from myapp.agents import rag

    return rag.answer(fixture["question"])
```

Each fixture is a JSON file under the scenarios directory; Inkfoot
reads it as a dict and hands it to `run()`.

## 2. Generate a baseline

Run the benchmark once on `main` and commit the artefact so CI has
something to diff against:

```bash
inkfoot benchmark ./tests/agent_scenarios --output baseline.json
git add baseline.json && git commit -m "Add Inkfoot benchmark baseline"
```

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

The action installs `inkfoot[all]` on the runner — which includes the
LangChain and LangGraph extras — runs the benchmark, diffs it against
the baseline, and posts a sticky PR comment. The full step-by-step and
the baseline-rotation options are in
[Set up CI cost review](set-up-ci.md#3-add-the-github-action).

## 4. Read the PR comment

Open a pull request that touches a prompt and the cost-review job
posts:

```markdown
## Inkfoot cost diff · ⚠️ warn

_Thresholds preset: **default** · baseline → current_

| Scenario | p50 Δ | p95 Δ | cache hit Δ | LLM calls Δ | Verdict |
|---|---|---|---|---|---|
| rag-qa | +41.3% | +37.0% | -1.1pp | +0.00 | ⚠️ warn |

### Regressions

- **rag-qa** — ⚠️ warn
  - p50 cost regressed by 41.3% (warn threshold +20.0%)
```

The defaults warn at +20% / fail at +50% on cost; the
[CLI reference](../reference/cli.md#inkfoot-diff) documents the full
threshold table.

## Two LangChain-specific tripwires

- **A new estimation flag appears.** If a PR switches a node to a
  streamed OpenAI Chat call without `stream_options`, the diff flags
  `stream_options_off` and the output number turns into an estimate.
  Treat a new flag as a signal to fix, not noise —
  [Spot streaming-cost surprises](streaming-cost-surprises.md) walks
  the fix.
- **The cost moved into one node.** When a regression is real, find
  *which* node owns it with
  [Find your most expensive LangChain node](find-expensive-langchain-node.md),
  then decide the fix there.

## Next step

The sticky comment's `Smell changes` section says *why* the cost
moved. If a new smell shows up and you don't recognise it:

→ [Cost Smells](../concepts/cost-smells.md) — the catalogue.
