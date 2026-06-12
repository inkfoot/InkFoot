# Quickstart

Five minutes from `pip install` to a rendered report — that's the
target. The whole flow is three steps: install, add one
`inkfoot.instrument()` call, wrap your agent in a `@agent_run`.

## 1. Install

```bash
pip install inkfoot
```

Requires Python 3.10+. Inkfoot is stdlib-friendly: the only hard
deps are [`tiktoken`](https://pypi.org/project/tiktoken/) (for
tokenisation) and [`python-ulid`](https://pypi.org/project/python-ulid/)
(for run identifiers). The provider SDKs (`anthropic`, `openai`)
are auto-detected — Inkfoot patches whichever is importable.

??? info "Optional extras"

    | Extra | Adds |
    |---|---|
    | `pip install "inkfoot[langgraph]"` | LangGraph framework adapter |
    | `pip install "inkfoot[openai-agents]"` | OpenAI Agents SDK adapter |
    | `pip install "inkfoot[anthropic-agent]"` | Anthropic Agent SDK adapter |
    | `pip install "inkfoot[all]"` | All framework adapters at once. This is the shape the `inkfoot/diff-action` GitHub Action installs in CI. |
    | `pip install "inkfoot[docs]"` | mkdocs-material toolchain for this site |

## 2. Instrument

Call `inkfoot.instrument()` once at process startup, before any
agent code runs. Top of `main()`, FastAPI's `lifespan`, or your
worker's startup hook are all fine.

```python
import inkfoot

inkfoot.instrument()
```

That single call:

- Monkey-patches `anthropic.Messages.create` (sync + async) and
  `openai.chat.completions.create` (sync + async). Every LLM call
  the SDK makes is recorded.
- Opens a local SQLite database at `~/.inkfoot/runs.db` (override
  with `INKFOOT_HOME=<dir>`).
- Starts a background thread that keeps run totals up to date.
- Registers an `atexit` hook so the database flushes cleanly on
  shutdown.

A second call is a no-op — the existing instrumentation stays in
place.

## 3. Wrap your work in a run

A *run* is one unit of agent work: handling a ticket, answering
a query, processing a document. Wrap each one with
`@inkfoot.agent_run(task=...)` so Inkfoot has somewhere to
attribute the LLM calls.

```python title="ticket_triage.py"
import inkfoot
import anthropic

inkfoot.instrument()

@inkfoot.agent_run(task="customer-support-triage")
def handle_ticket(ticket_id: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system="You triage customer-support tickets.",
        messages=[{"role": "user", "content": f"Triage ticket {ticket_id}."}],
    )
    inkfoot.set_outcome("success", quality_score=0.94)
    return response.content[0].text


if __name__ == "__main__":
    print(handle_ticket("TKT-1234"))
```

That's the entire agent. Run it once:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python ticket_triage.py
```

## 4. Look at the report

`inkfoot report` reads the same SQLite database the run wrote to.
Grab the run id from the previous output (or list recent runs
with `inkfoot report --last 1h`), then:

```bash
inkfoot report --run run-01JX...
```

You'll see something like:

```
Run run-01JX0E2QSV · customer-support-triage · 1.4s · $0.0007 · success (0.94)

Causal attribution:
  system_static       58.6%  ███████░░░░░  $0.0004
  user_input          24.1%  ███░░░░░░░░░  $0.0001
  output              17.3%  ██░░░░░░░░░░  $0.0001

Smells detected (0)
```

The headline number is the call cost. The bar chart splits that
across the [Causal Token Ledger](concepts/causal-token-ledger.md)
categories. The smells block stays empty on a clean run and fills
in the moment one fires. Re-run with a longer prompt or a
timestamp embedded in your system message and watch
[`unstable-prompt-prefix`](concepts/cost-smells.md#unstable-prompt-prefix)
light up.

## 5. Next steps

You've got a working baseline. Pick the next thread:

- [Find your most expensive agent](recipes/find-expensive-agent.md) —
  use the aggregate view to spot the worst offender across a week
  of runs.
- [Spot cache-miss patterns](recipes/spot-cache-misses.md) — the
  `unstable-prompt-prefix` smell and the report bar chart, together.
- [Set up CI cost review](recipes/set-up-ci.md) — wire `inkfoot
  benchmark` + `inkfoot diff` into a PR-time comment via the
  published GitHub Action.

## Troubleshooting

??? failure "`anthropic.AuthenticationError: No API key provided`"
    Inkfoot doesn't supply provider credentials. Export
    `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) before running.

??? failure "Report says `inkfoot report: no run with id ...`"
    Two common causes:

    1. **Typo in the run id.** Run `inkfoot report --last 1h` to
       list recent runs and copy the id from there.
    2. **Two databases.** Inkfoot writes to `~/.inkfoot/runs.db`
       by default. If your agent set `INKFOOT_HOME=<dir>` but the
       report didn't, they don't share storage. Pass `--db
       <path>/runs.db` to `inkfoot report`, or export the same
       `INKFOOT_HOME` in both shells.

??? failure "`inkfoot.errors.InkfootError: agent_run requires inkfoot.instrument()`"
    The decorator needs storage. Call `inkfoot.instrument()` once
    at startup *before* any decorated function runs. If you're
    using a multi-process server (Gunicorn, Celery), put the call
    in the worker bootstrap, not the parent process.

??? failure "Report shows `(no outcome)` even though my agent finished"
    `inkfoot.set_outcome("success" | "accepted_answer" | "failure" | "human_escalated")`
    has to fire before the run scope exits. Decorators wrap the
    function call exactly, so any `return` after `set_outcome`
    counts — but a swallowed exception path that skips both
    `set_outcome` and the implicit run-end can leave the row at
    `(no outcome)`. Add a `set_outcome` call to your error path or
    let the exception propagate (which Inkfoot records as
    `error`).

??? failure "`pip install inkfoot` fails on `tiktoken`"
    `tiktoken` is the heaviest dependency Inkfoot pulls in.
    Common fixes:

    - Upgrade `pip` (`python -m pip install --upgrade pip`).
    - On Apple Silicon / Linux ARM, install a Rust toolchain
      first: `rustup` (or `brew install rust`) — `tiktoken`'s
      wheel build needs it on systems without a prebuilt binary.

## Where to from here?

The fastest follow-up is the [aggregate view](recipes/find-expensive-agent.md).
For the design intent behind everything you saw above, jump to
the [Causal Token Ledger](concepts/causal-token-ledger.md)
concept page.
