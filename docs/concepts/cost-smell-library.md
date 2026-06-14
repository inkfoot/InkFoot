# Cost Smell Library

The [built-in smells](cost-smells.md) are eleven patterns that ship in
the box. The **Cost Smell Library** is the open, growing catalogue around
them: a public collection of smell definitions that anyone can contribute
to, browse on the web, and get bundled with the package.

There are three pieces to it: the **bundled snapshot** that ships inside
`inkfoot`, the **library site** you can browse, and the **open catalogue**
you can contribute to.

## The bundled snapshot

Every release of `inkfoot` ships a frozen snapshot of the catalogue at
`inkfoot/library/_snapshot.json`. It's bundled so the library is
available offline — no network call at import time — and it refreshes
whenever you upgrade:

```bash
pip install --upgrade inkfoot
```

Read it through the `inkfoot.library` API:

```python
from inkfoot.library import list_library_smells, get_library_smell

for smell in list_library_smells():
    print(smell.id, "—", smell.title, f"({smell.severity})")

prefix = get_library_smell("unstable-prompt-prefix")
print(prefix.recommendation)
print(prefix.suggested_policy)        # "CacheControlPlacer"
print(prefix.has_estimated_savings)   # False until a saving is measured
```

Each entry is a `LibrarySmell` with the smell's `id`, `title`,
`severity`, `description`, `detection` rule, `recommendation`, and
optional `suggested_policy`. The snapshot is the distributable
*catalogue*; the built-in detectors remain the authoritative detection
path for the smells they cover.

The snapshot is generated from the catalogue's source definitions and
validated against the catalogue schema at build time, so the copy in your
install is always well-formed.

## The library site

Every smell has its own page on
[library.inkfoot.dev](https://library.inkfoot.dev) — the detection rule,
why it costs money, the one-line fix, and (when available) an estimated
saving. The site is generated straight from the catalogue, so a smell
merged into the catalogue shows up on the site and, on the next release,
in your bundled snapshot.

## Estimated savings

Some smells carry an **estimated potential saving**: what you would save
if you applied the fix and nothing else about the run changed. Two fields
hold this — `estimated_savings` (the numbers) and `evidence_kind` (how
strong the evidence is):

| `evidence_kind` | Meaning |
|---|---|
| `simulation` | The saving is recomputed from anonymised run shapes — the recommendation is simulated against real ledgers. |
| `replay_pair` | A measured before/after on the same input. Stronger. |
| `production_pair` | A contributed, manually verified before/after. Strongest. |

These fields are filled in only by the savings-estimation pipeline, never
authored by hand. A smell with no number yet simply reads *savings: not
yet estimated* — it's still a real smell, just without a measured figure.
The current bundled snapshot ships before any savings have been
estimated, so every entry shows "not yet estimated".

## Contributing a smell

The catalogue is open. A smell is a single YAML file validated against a
frozen schema:

```yaml
id: my-org/spurious-system-edit
title: Spurious system-prompt edit
severity: warn
description: >-
  What the pattern is and why it costs money.
detection:
  language: jsonpath
  query: |
    $..ledger.system_dynamic_tokens
  trigger_condition: "value > 0.10"
recommendation: >-
  One actionable sentence.
suggested_policy: CacheControlPlacer
```

A lint bot checks every proposed smell against the schema automatically,
and the publish bar is high on purpose:

- the detection query runs in a single linear pass over a run's events;
- the smell ships at least three positive and three negative example
  runs;
- the recommendation is one actionable sentence;
- `suggested_policy` names a real [policy](observation-policies.md), or is
  `null`.

See the catalogue's `CONTRIBUTING` guide for the full process. Merged
smells appear on the library site immediately and in the bundled snapshot
on the next release.
