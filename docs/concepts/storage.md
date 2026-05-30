# Storage & Configuration

Inkfoot stores everything in a local SQLite database. This page covers
where the data lives, how to point Inkfoot at a different location,
which environment variables it honours, and what the bundled pricing
snapshot tells you.

## Where data lives

| Default | Description |
|---|---|
| `~/.inkfoot/runs.db` | The SQLite database file. Every run, event, and (when replay mode is on) full request/response body lands here. |

The directory is created on first use; you do not need to create it
yourself.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `INKFOOT_HOME` | `~/.inkfoot` | Parent directory of `runs.db`. Set this to relocate the entire Inkfoot storage area. |
| `INKFOOT_AGGREGATOR_INTERVAL_MS` | `500` | How often the background aggregator drains pending runs (milliseconds). Values below 10 ms are clamped to 10 ms with a warning. |

Set them before your process starts:

```bash
export INKFOOT_HOME=/var/lib/inkfoot
export INKFOOT_AGGREGATOR_INTERVAL_MS=200
python my_agent.py
```

## Tables you'll see

If you open the database with `sqlite3 ~/.inkfoot/runs.db`, you'll
find:

| Table | Holds |
|---|---|
| `runs` | One row per agent run: task, agent_kind, started_at, ended_at, status, outcome, quality_score, and projected totals (`total_input_tokens`, `total_output_tokens`, `total_cache_read_tokens`, `total_cache_creation_tokens`, `total_nanodollars`). |
| `events` | The append-only event log. One row per LLM call (`kind='llm_call'`), per policy warning, per `inkfoot.tag(...)` call, per `set_outcome`, and per run lifecycle event. |
| `event_contents` | Full request and response bodies — populated only when `inkfoot.instrument(capture_mode="replay")` is in effect. Empty by default. |
| `applied_migrations` | Inkfoot's own migration bookkeeping. |

The event log is the source of truth. The `runs.total_*` columns are
*projections* recomputable from the event log via `inkfoot
rebuild-aggregates`. You can safely query and read from either; never
modify the projection columns by hand (or if you do, run
`rebuild-aggregates` after to put them back in sync).

## Money type

All cost arithmetic uses integer **nanodollars** (10⁻⁹ USD). Token
prices sit well below a cent, so cents lose per-token precision and
floats drift across millions of tokens. A signed 64-bit integer in
nanodollars holds about $9.2 billion, which is ample for any single
workspace.

Helpers in `inkfoot.money`:

```python
from decimal import Decimal
from inkfoot.money import usd_to_nd, nd_to_usd, format_usd

usd_to_nd(Decimal("0.0004"))   # → 400000
nd_to_usd(400000)              # → Decimal("0.000400000")
format_usd(400000, decimals=4) # → "$0.0004"
```

`usd_to_nd` accepts `Decimal` and `int`. Floats are rejected at the
boundary with `TypeError` — they're a typed mistake we'd rather you
catch than silently drift on.

## Pricing snapshot

Inkfoot ships a static snapshot of per-token prices for every
supported model. The snapshot tells the report renderer how many
dollars a given token count costs. The current snapshot covers:

| Provider | Models |
|---|---|
| Anthropic | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5` |
| OpenAI | `gpt-4o`, `gpt-4o-mini`, `o1` |

For each model, four rates are recorded: `input`, `output`,
`cache_read`, and `cache_write` (OpenAI's `cache_write` is zero — they
don't bill for cache writes).

Unknown `(provider, model)` pairs are not an error — the report still
shows token counts but omits the dollar column. To check the active
snapshot revision:

```python
from inkfoot.pricing import PRICING_TABLE_REVISION, revision_date
print(PRICING_TABLE_REVISION)   # e.g. "2026-01-15"
print(revision_date())          # datetime.date(2026, 1, 15)
```

## Backups and portability

The database is a single SQLite file in WAL mode. Standard SQLite
backup techniques work:

```bash
# Cold-copy when no instrumented process is running
cp ~/.inkfoot/runs.db /backups/runs-$(date +%Y%m%d).db

# Hot-copy via SQLite's backup command
sqlite3 ~/.inkfoot/runs.db ".backup '/backups/runs-hot.db'"
```

The database is portable across machines that share the same SQLite
version family.

## Resetting

To start from a fresh database — every run, every event, gone:

```bash
rm -rf ~/.inkfoot/runs.db ~/.inkfoot/runs.db-wal ~/.inkfoot/runs.db-shm
```

Do this only when you're sure you don't need the history.

## Crash recovery

Inkfoot is designed to survive abrupt termination:

- The database runs in WAL mode with `synchronous=NORMAL`. Events
  written before the crash survive.
- A run that was `running` at crash time is auto-flipped to
  `status='error'` with `error_message='abandoned'` the next time
  Inkfoot starts up.
- Run totals may briefly disagree with the event log after a crash;
  `inkfoot rebuild-aggregates` brings them back in sync.

## Concurrency

The default storage is intended for a single instrumented process per
database. Multiple writers to the same SQLite file *can* work via
WAL's reader-writer concurrency, but a multi-process production
deployment is better served by a single instrumented entry point (e.g.
your worker process) writing to its own per-process database.
