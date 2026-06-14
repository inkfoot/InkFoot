# Cost Smell Library

A **cost smell** is a named pattern in an AI agent run's token
attribution that almost always means wasted money — a system prompt that
drifts and defeats the cache, a tool result recycled across a dozen
turns, a retry loop that re-tokenises the whole context on every attempt.

This site is the browsable catalogue. Each smell has its own page
describing what it detects, why it costs you, and the one change that
fixes it.

- **[Browse the catalogue →](catalogue.md)**

## Where these come from

The catalogue is built from an open repository of smell definitions —
one YAML file per smell. Anyone can propose a new smell by opening a pull
request; a schema lint bot checks it automatically, and a maintainer
reviews it against a high publish bar (cheap detection query, positive
and negative fixtures, a one-sentence fix).

The same catalogue ships bundled with the `inkfoot` package, so smell
detection works offline and refreshes when you upgrade.

## Savings estimates

Some smells carry an estimated potential saving — what you would save if
you applied the fix and nothing else about the run changed. These numbers
are computed against an anonymised corpus of real runs, never authored by
hand, and labelled with the strength of their evidence. A smell with no
number yet simply shows *savings: not yet estimated* — it is still a real
smell, just without a measured figure.
