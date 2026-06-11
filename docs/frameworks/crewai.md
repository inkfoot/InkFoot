# CrewAI

Inkfoot's CrewAI adapter wraps `Crew.kickoff` and hooks each
agent's and task's execute path, so one kickoff becomes one run
and every LLM call inside it carries `metadata.agent_name` and
`metadata.task_name` — the report can then slice a crew's cost per
agent or per task.

## Install

```bash
pip install "inkfoot[crewai]"
```

The extra pulls the matching `crewai` peer dependency.

## Instrument

```python
import inkfoot
import inkfoot.crewai
from crewai import Agent, Crew, Task

inkfoot.instrument()

crew = Crew(agents=[researcher, writer], tasks=[research, draft])

inkfoot.crewai.instrument(crew, task="research-pipeline")   # ← the one Inkfoot line
```

`inkfoot.crewai.instrument(crew)`:

1. Scopes a run around `crew.kickoff()` and `crew.kickoff_async()`
   so the whole crew execution attributes to one run. Kickoffs
   made inside an outer `inkfoot.agent_run` block join that run.
2. Hooks every agent's `execute_task` (including a
   `manager_agent`, when the crew has one) so LLM calls made while
   that agent works are stamped with `metadata.agent_name` — the
   agent's `name` if set, else its `role`.
3. Hooks every task's execute path so the same calls also carry
   `metadata.task_name` — the task's `name` if set, else its
   description (whitespace-collapsed, truncated to 80 chars).

The hooks nest correctly: when a manager delegates to a worker
agent, calls made during the delegation attribute to the worker,
and attribution returns to the manager when the delegation ends.

## What you get

### Per-agent cost slices

```bash
inkfoot report --run run-01JX0... --group-by metadata.agent_name
```

```
Run run-01JX0... · research-pipeline · per-agent_name ledger

  agent_name               calls  input_tok  output_tok       cost
  Researcher                   6      14204        4000    $0.0214
  Writer                       3       7810        2000    $0.0125
```

Sorted by spend descending so the most expensive agent lands at
the top.

"Which agent in the crew eats the budget?" is one flag away.
`--group-by metadata.task_name` gives the same table sliced per
task instead.

### Observation-only by design

CrewAI's LLM calls still pass through the instrumented provider
SDK — that's how the attribution metadata lands — but CrewAI
assembles each request from internal state and doesn't expose the
stable per-turn context (tool registry, turn boundaries) that
request-modification needs; an external rewrite could silently
desync the crew's own bookkeeping. The adapter therefore declares
**no**
[modification policies](../concepts/modification-policies.md):
registering `LazyToolExposure` or `CheapSummariser` against it
raises `PolicyNotSupported` up front rather than silently doing
nothing. Observation policies (e.g. `BudgetCap`) work as usual.

## Worked example

```python
import inkfoot
import inkfoot.crewai
from crewai import Agent, Crew, Task

inkfoot.instrument()

researcher = Agent(
    role="Researcher",
    goal="Collect accurate background facts",
    backstory="A meticulous fact-finder.",
)
writer = Agent(
    role="Writer",
    goal="Turn research into a crisp summary",
    backstory="A concise technical writer.",
)

research = Task(
    description="Gather three facts about squid ink.",
    expected_output="Three sourced bullet points.",
    agent=researcher,
)
draft = Task(
    description="Write a 100-word summary from the research.",
    expected_output="A 100-word paragraph.",
    agent=writer,
)

crew = Crew(agents=[researcher, writer], tasks=[research, draft])
inkfoot.crewai.instrument(crew, task="ink-research")

result = crew.kickoff()
```

After one kickoff, `inkfoot report --run <id>` shows the crew's
14-field ledger; `--group-by metadata.agent_name` splits it
between `Researcher` and `Writer`, and
`--group-by metadata.task_name` splits it between the two tasks.

## Async + variants

`Crew.kickoff_async` is wrapped symmetrically. Note that CrewAI's
`kickoff_for_each(...)` executes on internal *copies* of the crew,
which the adapter on your instance can't see — instrument inside
the loop (or call `kickoff` per input yourself) if you need each
iteration attributed:

```python
for inputs in batches:
    crew_copy = crew.copy()
    inkfoot.crewai.instrument(crew_copy, task="ink-research")
    crew_copy.kickoff(inputs=inputs)
```

## Where to next

- [`inkfoot report`](../reference/cli.md) — the single-run view's
  `--group-by metadata.<key>` works with any adapter-stamped
  metadata, not just CrewAI's.
- [Find your most expensive agent](../recipes/find-expensive-agent.md) —
  the aggregate view ranks whole runs; the per-agent slice here
  drills into one crew run.
- [Pydantic AI](pydantic-ai.md) — single-agent loops with
  per-tool dispatch events.
