# Contributing a cost smell

A **cost smell** is a named pattern in an agent run's token attribution
that almost always means wasted money. This repository is the open
catalogue of those smells. Anyone can propose a new one by opening a
pull request that adds a single YAML file under [`smells/`](smells/).

Every pull request is checked automatically by the lint bot
([`.github/workflows/lint.yml`](.github/workflows/lint.yml)), which runs
[`tools/validate_smells.py`](tools/validate_smells.py) against the frozen
schema in [`schema/smell.schema.json`](schema/smell.schema.json). Get the
bot green, and a maintainer reviews against the publish bar below.

## The publish bar

A smell is merged when it meets all four criteria. The bar is high on
purpose: a small, sharp catalogue is worth far more than a large, noisy
one, and a tight bar keeps review fast.

1. **The detection query is cheap.** It must terminate in
   O(events × constant) over a single run's event stream — one linear
   pass, no cross-joins, no implicit comma-joins, no nested recursive
   scans. The lint bot rejects the obvious pathological shapes; reviewers
   reject the subtle ones.
2. **It ships fixtures.** At least **3 positive** fixtures (runs that
   should trigger the smell) and **3 negative** fixtures (runs that look
   similar but should stay silent), under
   `fixtures/<smell-id>/positive/` and `fixtures/<smell-id>/negative/`.
   The negatives are the important half: they prove the rule doesn't fire
   on innocent runs. Include a boundary case (a run sitting right at the
   threshold) among them.
3. **The recommendation is one actionable sentence.** A reader should
   know what to change without further research. "Move time-varying
   content out of the system block" — not "consider reviewing your
   prompt structure".
4. **`suggested_policy` names a real policy, or is `null`.** If a policy
   remediates the smell, name it. If none does yet, set `null` — don't
   invent one.

## The smell file

One smell per file, named `smells/<id>.yaml`. The full contract is the
[JSON Schema](schema/smell.schema.json); here is the shape:

```yaml
id: my-org/spurious-system-edit        # lower-case kebab-case; community smells may use an "owner/" prefix
title: Spurious system-prompt edit
severity: warn                          # info | warn | critical
description: >-
  One short paragraph: what the pattern is and why it costs money.

detection:
  language: jsonpath                    # jsonpath | sql
  query: |
    $..ledger.system_dynamic_tokens
  trigger_condition: "value > 0.10"

recommendation: >-
  One actionable sentence.

suggested_policy: CacheControlPlacer    # a real policy name, or null
primary_category: system_dynamic_tokens # optional; the ledger field the smell explains, or null
```

### Reserved fields — do not set these

`estimated_savings` and `evidence_kind` are **filled in by the
savings-estimation worker, never by hand**. The worker runs a smell's
recommendation against an anonymised corpus and writes back the measured
potential saving. A pull request that sets either field is rejected by
the lint bot — you can't claim a saving the corpus hasn't measured. New
smells merge as *candidates* with no savings number; the number lands
later, automatically, once there is evidence for it.

### `language: builtin` is reserved

Smells whose detector ships inside the `inkfoot` package use
`language: builtin`, where the `query` is documentary. Community
contributions use `jsonpath` or `sql` so the detection rule is fully
expressed in the file.

## Validate locally before opening a PR

```bash
pip install pyyaml jsonschema
python tools/validate_smells.py                  # check every smell
python tools/validate_smells.py smells/my-smell.yaml
python tools/validate_smells.py --require-fixtures
```

## What happens after merge

A merged smell appears on [library.inkfoot.dev](https://library.inkfoot.dev)
and is picked up into the snapshot that ships bundled with the `inkfoot`
package on its next release, so it works offline for every user. Savings
estimates are added later by the worker as evidence accumulates.

By contributing you agree that your contribution is licensed under the
project's [Apache 2.0 license](LICENSE).
