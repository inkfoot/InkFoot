# Postgres Backend

The default SQLite backend is the right choice for a single
instrumented process — zero setup, one file, easy backups. When
several processes need to write to the same event log (a fleet of
workers, a web app plus a batch job), point Inkfoot at a shared
Postgres server instead.

What changes when you switch:

- All processes write to one database over the network, so the
  whole fleet's runs land in one event log.
- Aggregation moves out of process: instead of the background thread
  each process runs against SQLite, a single
  [`inkfoot aggregator-worker`](../reference/cli.md#inkfoot-aggregator-worker)
  daemon projects run totals for the whole fleet.
- Everything else — the event log, the ledger, replay capture —
  behaves identically. The schema mirrors the SQLite one table for
  table.

One caveat to plan around: the local CLI commands (`inkfoot
report`, `tag`, `tail`, `rebuild-aggregates`, `contract draft`)
read a SQLite file only — they do not honour `INKFOOT_PG_DSN` yet,
so they can't see runs recorded in Postgres. Query the database
directly for fleet-wide questions.

## Installing

The Postgres driver ships as an optional extra:

```bash
pip install "inkfoot[postgres]"
```

This pulls in [psycopg 3](https://www.psycopg.org/psycopg3/) and its
connection pool. Without the extra, constructing the backend raises a
`StorageError` that names the missing package.

## Pointing Inkfoot at Postgres

Pass a `PostgresStorage` instance to `inkfoot.instrument()`:

```python
import inkfoot
from inkfoot.storage import PostgresStorage

inkfoot.instrument(
    storage=PostgresStorage(dsn="postgresql://app@db.internal/inkfoot"),
)
```

Or leave the DSN to the environment:

```bash
export INKFOOT_PG_DSN=postgresql://app@db.internal/inkfoot
```

```python
inkfoot.instrument(storage=PostgresStorage())
```

On first connect the backend creates its schema with forward-only
migrations. Migration runs are guarded by a Postgres advisory lock,
so many processes starting at once is safe — one applies the DDL,
the rest wait and see it already applied.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `INKFOOT_PG_DSN` | — | Connection string used when `PostgresStorage()` is constructed without an explicit `dsn=`. |
| `INKFOOT_PG_POOL_MIN` | `1` | Minimum connections kept in the per-process pool. |
| `INKFOOT_PG_POOL_MAX` | `4` | Maximum connections the pool may open. |
| `INKFOOT_AGGREGATOR_INTERVAL_MS` | `500` | Sweep cadence of `inkfoot aggregator-worker` (same variable the SQLite in-process aggregator honours). |

Constructor arguments (`PostgresStorage(pool_min=..., pool_max=...)`)
win over the environment. A malformed pool variable is ignored with
a warning; an environment minimum above the maximum raises the
maximum to match (also with a warning).

## The aggregation worker

With SQLite, every instrumented process runs a small background
thread that projects `runs.total_*` columns from the event log. With
Postgres that would mean N processes racing to do the same work, so
the backend disables the in-process thread and expects a separate
daemon:

```bash
inkfoot aggregator-worker --dsn postgresql://app@db.internal/inkfoot
```

Run it under your process supervisor of choice (systemd, a k8s
Deployment, a Docker sidecar). It sweeps dirty runs on the
`INKFOOT_AGGREGATOR_INTERVAL_MS` cadence and records a heartbeat
after every sweep.

You can run **more than one** worker for availability. Each sweep is
wrapped in a Postgres advisory lock, so exactly one worker sweeps at
a time while the others wait their turn. Because the lock is tied to
the holder's database session, a worker that dies — even `kill -9` —
releases it automatically, and a standby takes over within about a
second. No fencing tokens, no leader election to operate.

### Liveness probe

The worker writes a heartbeat row after every sweep (including empty
ones, so an idle-but-healthy worker stays green). `--health` reads
it and exits 0/1:

```bash
inkfoot aggregator-worker --health --max-age-s 60
# aggregator-worker: last sweep at 2026-06-12T09:14:03.512Z (2.1s ago), 17 runs swept
```

Exit code 1 means no heartbeat yet or a heartbeat older than
`--max-age-s` (default 60). Wire it into a k8s liveness probe or a
monitoring check. For one-shot use — cron jobs, debugging — `--once`
acquires the lock, runs a single sweep, and exits.

## Migrating from SQLite

`inkfoot migrate` copies an existing SQLite database into Postgres,
in batches, with progress on stderr:

```bash
inkfoot migrate --to postgres \
  --db ~/.inkfoot/runs.db \
  --dsn postgresql://app@db.internal/inkfoot
```

The intended cutover flow:

1. **Quiesce writers** — stop the instrumented processes pointing at
   the SQLite file. The copy reads a consistent snapshot; writes that
   land mid-copy are not picked up.
2. **Run the migration.** It applies the Postgres schema, copies
   `runs`, `events`, and `event_contents` in batches, verifies the
   row counts, runs `VACUUM ANALYZE`, and only then renames the
   source file to `<name>.migrated` (WAL sidecars too). The source
   is never deleted.
3. **Repoint and restart** — switch your processes to
   `PostgresStorage`, start an `aggregator-worker`, and bring the
   fleet back up.

Useful properties:

- **Resumable.** Batches commit independently. If the migration is
  interrupted (Ctrl-C, OOM, network blip), re-run the same command —
  it picks up after the last committed batch instead of starting
  over, and `ON CONFLICT DO NOTHING` inserts make any overlap
  harmless.
- **Verified before destructive-ish steps.** The source rename only
  happens after the copied row counts check out. A failed
  verification leaves the SQLite file untouched and tells you to
  re-run.
- **Idempotent once done.** Re-running after success is a no-op that
  says so and exits 0.

Batch sizes are tunable (`--runs-batch`, default 1000;
`--events-batch`, default 10000) but the defaults are sized so that
event logs in the hundred-thousand range migrate in well under a
minute.

For the day-of checklist — provisioning, backups, verification,
service supervision for the worker, and the rollback path — follow
the [Postgres migration runbook](../operations/postgres-migration.md).

## Tables you'll see

The Postgres schema mirrors the
[SQLite one](storage.md#tables-youll-see): `runs`, `events`,
`event_contents`, and `applied_migrations`, with the same columns
and the same event-log-is-source-of-truth contract. One addition is
`aggregator_heartbeat`, a single-row table backing the worker's
`--health` probe.

Backups are standard Postgres operations — `pg_dump`, WAL archiving,
or your provider's snapshots.
