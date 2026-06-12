# Postgres migration runbook

Step-by-step operational guide for moving a working SQLite
deployment onto the [Postgres backend](../concepts/postgres.md).
That page covers the architecture (what changes, the aggregation
worker, environment variables); this one is the checklist you
follow on the day.

## Before you start

**What moves.** `inkfoot migrate` copies the `runs`, `events`, and
`event_contents` tables. Run totals are re-projected on the Postgres
side by the aggregation worker, so they don't need to be current in
the source.

**What changes operationally.**

- Every instrumented process needs `INKFOOT_PG_DSN` (or an explicit
  `PostgresStorage(dsn=...)`) and the `inkfoot[postgres]` extra.
- Aggregation moves out of process: you now run at least one
  [`inkfoot aggregator-worker`](../reference/cli.md#inkfoot-aggregator-worker)
  daemon. Without it, `runs.total_*` columns stay stale.

**Plan around this limitation.** `inkfoot report`, `tag`, `tail`,
`rebuild-aggregates`, and `contract draft` read a local SQLite file
only — they do not honour `INKFOOT_PG_DSN` yet. After cutover,
those commands cannot see the fleet's new runs; query Postgres
directly for fleet-wide questions, and keep the renamed source file
around for historical reports (`inkfoot report --db
~/.inkfoot/runs.db.migrated ...` still works).

**Prerequisites.**

- [ ] A reachable Postgres server, and a role that may create
      tables in the target database (the schema is applied by
      forward-only migrations on first connect).
- [ ] `pip install "inkfoot[postgres]"` on the host running the
      migration and on every instrumented host.
- [ ] A maintenance window in which agent writers can be stopped.
      The copy reads a consistent snapshot — rows written mid-copy
      are not picked up.

**Sizing.** Default batch sizes (`--runs-batch 1000`,
`--events-batch 10000`) move event logs in the hundred-thousand
range in well under a minute. The window you need is dominated by
stopping and restarting your agents, not the copy.

## 1. Provision the database

```bash
createdb inkfoot
psql -c "CREATE ROLE app LOGIN PASSWORD '...'"
psql -d inkfoot -c "GRANT ALL ON SCHEMA public TO app"
```

Confirm connectivity from the migrating host with the DSN you'll
use everywhere:

```bash
export INKFOOT_PG_DSN=postgresql://app@db.internal/inkfoot
psql "$INKFOOT_PG_DSN" -c "SELECT 1"
```

No manual DDL: the migration applies the schema itself, guarded by
an advisory lock, so concurrent first-connectors are safe.

## 2. Back up and quiesce writers

Stop every process that writes to the SQLite file, then take a
copy — including the `-wal` / `-shm` sidecars if present:

```bash
cp ~/.inkfoot/runs.db*  /backup/inkfoot-$(date +%F)/
```

The migration never deletes the source, but a backup makes the
rollback step a file copy instead of a judgement call.

## 3. Run the migration

```bash
inkfoot migrate --to postgres --db ~/.inkfoot/runs.db \
  --dsn "$INKFOOT_PG_DSN"
```

Progress goes to stderr batch by batch; a summary prints at the
end. What happens, in order: schema applied, `runs` copied, `events`
and `event_contents` copied, row counts verified, `VACUUM ANALYZE`,
and only then the source file is renamed to `runs.db.migrated`
(sidecars too).

If the run is interrupted — Ctrl-C, OOM, network blip (exit code
`130` or `1`) — re-run the same command. Batches commit
independently and inserts are `ON CONFLICT DO NOTHING`, so the copy
resumes after the last committed batch. Re-running after success is
a no-op that exits `0`. Flags and exit codes:
[`inkfoot migrate`](../reference/cli.md#inkfoot-migrate).

## 4. Verify

The command verifies row counts before renaming the source, so a
completed run is already checked. For belt and braces, compare
counts yourself:

```bash
sqlite3 ~/.inkfoot/runs.db.migrated \
  "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM events)"
psql "$INKFOOT_PG_DSN" -c \
  "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM events)"
```

and spot-check a known run id:

```bash
psql "$INKFOOT_PG_DSN" -c "SELECT id, task, status FROM runs LIMIT 5"
```

A failed verification leaves the SQLite file untouched (no rename)
and asks you to re-run.

## 5. Start the aggregation worker

Smoke-test one sweep, then install the daemon under your process
supervisor:

```bash
inkfoot aggregator-worker --once
```

```ini title="/etc/systemd/system/inkfoot-aggregator.service"
[Unit]
Description=Inkfoot aggregation worker
After=network-online.target

[Service]
Environment=INKFOOT_PG_DSN=postgresql://app@db.internal/inkfoot
ExecStart=/opt/venvs/agents/bin/inkfoot aggregator-worker
Restart=always

[Install]
WantedBy=multi-user.target
```

You may run more than one worker for availability — sweeps are
serialised by a Postgres advisory lock, and a dead leader's lock
releases with its session, so a standby takes over within about a
second.

Wire the heartbeat into your monitoring (exit `0` healthy, `1`
stale or absent):

```bash
inkfoot aggregator-worker --health --max-age-s 60
```

```yaml title="Kubernetes liveness probe"
livenessProbe:
  exec:
    command: ["inkfoot", "aggregator-worker", "--health", "--max-age-s", "60"]
  periodSeconds: 30
```

## 6. Cut the fleet over

On every instrumented host: install the extra, set the DSN, and
restart with `PostgresStorage`:

```bash
export INKFOOT_PG_DSN=postgresql://app@db.internal/inkfoot
```

```python
import inkfoot
from inkfoot.storage import PostgresStorage

inkfoot.instrument(storage=PostgresStorage())
```

Confirm the fleet is live: run counts grow, and the worker
heartbeat reports recent sweeps.

```bash
psql "$INKFOOT_PG_DSN" -c "SELECT count(*) FROM runs"
inkfoot aggregator-worker --health
```

## 7. Roll back (if you must)

1. Stop the instrumented processes and the aggregation worker.
2. Restore the SQLite file: rename `runs.db.migrated` back to
   `runs.db` (or restore from the step-2 backup).
3. Remove `INKFOOT_PG_DSN` / revert the `storage=` argument, and
   restart the fleet on SQLite.

Runs recorded in Postgres during the trial are **not** merged back
into the SQLite file — they remain queryable in Postgres, but the
rolled-back fleet won't see them. If you later retry the migration
from the same source, the resumable `ON CONFLICT DO NOTHING` copy
makes the overlap harmless.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `StorageError` naming `psycopg` | The `postgres` extra isn't installed on this host: `pip install "inkfoot[postgres]"`. |
| "source database not found" on re-run | The previous run completed and renamed the file to `<name>.migrated`. Nothing to do. |
| Verification failure | A writer was still appending during the copy. Quiesce properly and re-run — the source was not renamed. |
| Totals all zero after cutover | The aggregation worker isn't running (or can't reach the database). Check `inkfoot aggregator-worker --health`. |
| `inkfoot report` shows no new runs | Expected: `report` reads local SQLite only. Use the renamed file for history and query Postgres for fleet-wide questions. |
