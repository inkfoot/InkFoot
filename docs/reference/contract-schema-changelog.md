# Token Contract schema changelog

This is the append-only record of changes to the
[Token Contract](../concepts/token-contracts.md) schema. Every contract
file declares a `schema_version`; the loader accepts the current
version and the one immediately before it, so this page is where you
look when a deprecation warning tells you to migrate.

## Versioning and deprecation policy

- The loader accepts the **current** schema version and the
  **immediately preceding** version (`current - 1`).
- Loading a `current - 1` contract logs a one-time deprecation warning
  per process, pointing here.
- A version older than `current - 1` is rejected with a "migrate your
  contracts" error.
- A version newer than the build supports is rejected with an "upgrade
  inkfoot" error.
- The deprecation window for a retired version is **six months** from
  the release that introduced its successor, giving teams time to
  migrate before an upgrade forces it.

## Version history

### Schema version 1

Initial release. A contract supports:

- `schema_version` — required integer.
- `task` — required string; the task name enforcement is keyed on.
- `cheap_model` — optional string; the fallback model a
  `switch_to_cheap_model` degrade step switches to.
- `budget` — optional clause with `max_nanodollars`, `max_llm_calls`,
  `max_tool_result_tokens`, `cache_hit_rate_min`, and
  `max_run_duration_seconds`.
- `outcome` — optional clause with `required_success_rate` and
  `measure_window_runs` (default 100).
- `degrade` — optional ladder of `{at_percent, action}` steps, where
  `action` is one of `warn`, `switch_to_cheap_model`, or `block`.
- `overrides` — optional per-tier map layering `budget`/`outcome`
  clauses over the base, resolved from the run's
  `metadata["tenant_tier"]`.

There is no predecessor to migrate from.

## Migrating between versions

When a new schema version ships, this section will carry a step-by-step
migration note for each bump. Until then, version 1 is the only
supported schema and no migration is required.
