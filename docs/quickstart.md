# Quickstart

Five minutes from `pip install` to a rendered report — that's the
target. The whole flow is three steps: install, add one
`inkfoot.instrument()` call, wrap your agent in a `@agent_run`.

This quickstart leads with **LangChain**, the most common way agents
reach a model. Calling a provider SDK directly instead? Everything
below still applies — swap step 3 for the
[Raw Provider SDK](frameworks/raw-sdk.md) shape and read on.

## 1. Install

```bash
pip install "inkfoot[langchain]" langchain-anthropic
```

Requires Python 3.10+. The `[langchain]` extra pins only
`langchain-core` — the package that defines the callback interface
Inkfoot registers against. Your provider partner package
(`langchain-anthropic` here, or `langchain-openai`,
`langchain-google-genai`, `langchain-aws`, …) stays yours to choose
and version.

??? info "Optional extras"

    | Extra | Adds |
    |---|---|
    | `pip install "inkfoot[langchain]"` | LangChain callback handler (auto-registers on `instrument()`) |
    | `pip install "inkfoot[langgraph]"` | LangGraph framework adapter (per-node attribution) |
    | `pip install "inkfoot[openai-agents]"` | OpenAI Agents SDK adapter |
    | `pip install "inkfoot[anthropic-agent]"` | Anthropic Agent SDK adapter |
    | `pip install "inkfoot[all]"` | All framework adapters at once. This is the shape the `inkfoot/diff-action` GitHub Action installs in CI. |
    | `pip install "inkfoot[docs]"` | mkdocs-material toolchain for this site |

## 2. Instrument

Call `inkfoot.instrument()` once at process startup, before any agent
code runs. Top of `main()`, FastAPI's `lifespan`, or your worker's
startup hook are all fine.

```python
import inkfoot

inkfoot.instrument()
```

That single call:

- Registers the [LangChain callback handler](concepts/langchain-integration.md)
  globally when `langchain-core` is importable, so every chat-model
  call through any chain, agent, or LCEL pipeline is captured — no
  `callbacks=` plumbing.
- Monkey-patches the provider SDKs too (`anthropic.Messages.create`,
  `openai.chat.completions.create`, and `openai.responses.create`,
  sync + async). When a LangChain call lands on a patched SDK, the two
  sightings [deduplicate](concepts/langchain-integration.md#why-that-doesnt-double-count)
  to one event.
- Opens a local SQLite database at `~/.inkfoot/runs.db` (override with
  `INKFOOT_HOME=<dir>`).
- Starts a background thread that keeps run totals up to date, and
  registers an `atexit` hook so the database flushes cleanly on
  shutdown.

A second call is a no-op — the existing instrumentation stays in
place. The explicit form `inkfoot.langchain.instrument()` registers
just the handler, if you want it without patching the raw SDKs.

!!! tip "Running in production?"

    The SQLite default assumes one writer — perfect for this
    walkthrough, not for a multi-worker service. When you scale past
    one process (gunicorn, Celery, multi-replica Kubernetes), or you
    turn on full request/response capture, read
    [Services & multi-replica deployments](operations/services-and-multi-replica.md).
    It covers OTel export vs. the Postgres backend and why replay
    capture needs a redaction hook first.

## 3. Wrap your work in a run

A *run* is one unit of agent work: handling a ticket, answering a
query, processing a document. Wrap each one with
`@inkfoot.agent_run(task=...)` so Inkfoot has somewhere to attribute
the LLM calls.

```python title="ticket_triage.py"
import inkfoot
from langchain_anthropic import ChatAnthropic

inkfoot.instrument()

model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=512)


@inkfoot.agent_run(task="customer-support-triage")
def handle_ticket(ticket_id: str) -> str:
    reply = model.invoke(
        "You triage customer-support tickets. "
        f"Triage ticket {ticket_id}."
    )
    inkfoot.set_outcome("success", quality_score=0.94)
    return reply.content


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
Grab the run id from the previous output (or list recent runs with
`inkfoot report --last 1h`), then:

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

The headline number is the call cost. The bar chart splits that across
the [Causal Token Ledger](concepts/causal-token-ledger.md) categories.
The smells block stays empty on a clean run and fills in the moment one
fires. Re-run with a longer prompt or a timestamp embedded in your
system message and watch
[`unstable-prompt-prefix`](concepts/cost-smells.md#unstable-prompt-prefix)
light up.

## 5. Next steps

You've got a working baseline. Pick the next thread:

- [Find your most expensive LangChain node](recipes/find-expensive-langchain-node.md) —
  break a LangGraph run down by node and find the one burning the
  budget.
- [Spot streaming-cost surprises](recipes/streaming-cost-surprises.md) —
  diagnose the `stream_options_off` estimation flag.
- [Wire CI cost review for a LangChain repo](recipes/ci-cost-review-langchain.md) —
  catch a prompt change that doubles the bill on the pull request
  that introduces it.

## Troubleshooting

??? failure "My LangChain calls aren't showing up"
    Two common causes:

    1. **`langchain-core` wasn't importable when `instrument()`
       ran.** The handler only auto-registers when the package is
       present. Install the `[langchain]` extra and confirm the
       import works in the same environment that runs your agent.
    2. **`instrument()` ran in a different process.** With a
       multi-process server (Gunicorn, Celery), call it in the worker
       bootstrap, not the parent.

??? failure "`AuthenticationError: No API key provided`"
    Inkfoot doesn't supply provider credentials. Export
    `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) before running — the
    error is raised by the provider SDK underneath LangChain, not by
    Inkfoot.

??? failure "Report says `inkfoot report: no run with id ...`"
    Two common causes:

    1. **Typo in the run id.** Run `inkfoot report --last 1h` to list
       recent runs and copy the id from there.
    2. **Two databases.** Inkfoot writes to `~/.inkfoot/runs.db` by
       default. If your agent set `INKFOOT_HOME=<dir>` but the report
       didn't, they don't share storage. Pass `--db <path>/runs.db`
       to `inkfoot report`, or export the same `INKFOOT_HOME` in both
       shells.

??? failure "`inkfoot.errors.InkfootError: agent_run requires inkfoot.instrument()`"
    The decorator needs storage. Call `inkfoot.instrument()` once at
    startup *before* any decorated function runs. If you're using a
    multi-process server (Gunicorn, Celery), put the call in the
    worker bootstrap, not the parent process.

??? failure "Report shows `(no outcome)` even though my agent finished"
    `inkfoot.set_outcome("success" | "accepted_answer" | "failure" | "human_escalated")`
    has to fire before the run scope exits. Decorators wrap the
    function call exactly, so any `return` after `set_outcome` counts
    — but a swallowed exception path that skips both `set_outcome` and
    the implicit run-end can leave the row at `(no outcome)`. Add a
    `set_outcome` call to your error path or let the exception
    propagate (which Inkfoot records as `error`).

??? failure "`pip install inkfoot` fails on `tiktoken`"
    `tiktoken` is the heaviest dependency Inkfoot pulls in. Common
    fixes:

    - Upgrade `pip` (`python -m pip install --upgrade pip`).
    - On Apple Silicon / Linux ARM, install a Rust toolchain first:
      `rustup` (or `brew install rust`) — `tiktoken`'s wheel build
      needs it on systems without a prebuilt binary.

## Where to from here?

The fastest follow-up is the
[aggregate view](recipes/find-expensive-agent.md). For how the
LangChain capture actually works — the two capture layers and the one
accuracy caveat — read
[the LangChain integration model](concepts/langchain-integration.md).
For the design intent behind the report, jump to the
[Causal Token Ledger](concepts/causal-token-ledger.md) concept page.
