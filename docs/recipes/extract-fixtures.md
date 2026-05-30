# Fixture Extraction

`scripts/extract_run_fixtures.py` is a small command-line tool that
exports recent LLM-call events from your Inkfoot database as JSON
fixtures. It's the workhorse behind two common workflows:

- **Building your own validation corpus** — harvest a representative
  sample of real production traffic, then hand-label it for the
  [Validation Harness](../concepts/accuracy.md).
- **Offline debugging** — pull yesterday's runs onto your laptop and
  reproduce the translator or smell engine's behaviour without
  needing the live database.

The script is privacy-aware by default: it exports the structured
ledger and call metadata, but does **not** include the request or
response bodies unless you explicitly opt in.

## Running it

### Last 24 hours, metadata only (default)

```bash
python scripts/extract_run_fixtures.py
```

Reads from `~/.inkfoot/runs.db`, exports every `llm_call` event from
the last 24 hours into `tests/fixtures/internal/`.

### Custom window and output directory

```bash
python scripts/extract_run_fixtures.py \
    --since 7d \
    --output ./harvested/
```

### Since a specific date

```bash
python scripts/extract_run_fixtures.py --since 2026-05-20
```

### Include full request / response bodies

```bash
python scripts/extract_run_fixtures.py \
    --since 24h \
    --include-content
```

Only meaningful when the corresponding runs were recorded under
`inkfoot.instrument(capture_mode="replay")` — the script reads from
the `event_contents` sibling table, which is empty in metadata mode.

### Skip in-progress runs

```bash
python scripts/extract_run_fixtures.py --complete-only
```

By default the script exports events from runs of *any* status — even
ones still in flight, which is useful when debugging a hang or a
mid-run cost spike. Pass `--complete-only` to restrict to runs that
have ended (`status='complete'` or `'error'`); typical for nightly
cron jobs.

## All flags

| Flag | Default | Purpose |
|---|---|---|
| `--db <path>` | `~/.inkfoot/runs.db` | Source database. |
| `--since <window>` | `24h` | Window cutoff. Accepts relative duration (`30m`, `24h`, `7d`) or absolute ISO date (`2026-05-20` = UTC midnight). |
| `--output <dir>` | `tests/fixtures/internal/` | Output directory. Created if missing. |
| `--include-content` | off | Also export request and response bodies. Requires the runs to have been recorded with `capture_mode="replay"`. |
| `--complete-only` | off | Skip runs with `status='running'`. |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Wrote fixtures successfully. |
| `2` | Bad arguments, missing database, or other usage error. |

## Output format

One JSON file per `llm_call` event, named:

```
<provider>-<model>-<run_id>-seq<sequence>.json
```

Example: `anthropic-claude-sonnet-4-6-run-01JZX0...-seq0001.json`

The file shape mirrors what the [Validation Harness](../concepts/accuracy.md)
expects:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "request": {},
  "response": {},
  "ledger_snapshot": {
    "system_static_tokens": 124,
    "system_dynamic_tokens": 0,
    "user_input_tokens": 42,
    "tool_schema_tokens": 0,
    "tool_result_tokens": 0,
    "retrieved_context_tokens": 0,
    "memory_tokens": 0,
    "retry_overhead_tokens": 0,
    "summariser_tokens": 0,
    "reasoning_tokens": 0,
    "guardrail_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_read_tokens": 0,
    "output_tokens": 28
  },
  "tools_offered": [],
  "tools_called": [],
  "cache_status": "n/a",
  "estimated_nanodollars": 432000
}
```

| Field | Notes |
|---|---|
| `provider` / `model` | Lifted from the recorded event payload. |
| `request` / `response` | Empty by default. Populated from the `event_contents` sibling table when `--include-content` is passed and the run was recorded under replay mode. |
| `ledger_snapshot` | The fourteen ledger fields exactly as the translator produced them at call time. |
| `tools_offered` / `tools_called` | Tool names recorded for the call. |
| `cache_status` | `hit`, `partial`, `miss`, or `n/a`. |
| `estimated_nanodollars` | Cost estimate at call time, or `null` for unknown `(provider, model)` pairs. |

The two-key separation matters for the validation harness:
`ledger_snapshot` is *what the translator produced* (suitable as a
snapshot label) while `request` / `response` (with `--include-content`)
are *what the translator saw* (suitable for re-running the translator
under a code change).

## Privacy posture

The default (no `--include-content`) writes only structured numbers
and tool names — never prompt text, never user input, never tool
output. This is safe to share within your team without further
review.

`--include-content` writes the full conversation bodies to disk.
Treat the output directory as you would the prompts themselves.

The script only reads from `event_contents` when both:

1. `--include-content` is passed, AND
2. The corresponding event was recorded under `capture_mode="replay"`.

If your process ran with `capture_mode="metadata"` (the default),
`--include-content` has nothing to read and the `request` /
`response` fields stay empty.

## Typical workflows

### Nightly archive

```bash
# Cron entry: harvest yesterday's complete runs at 02:00 UTC
0 2 * * *  python /opt/inkfoot/scripts/extract_run_fixtures.py \
               --since 24h \
               --complete-only \
               --output /var/lib/inkfoot/fixtures/$(date +\%Y-\%m-\%d)
```

### Building a validation corpus

```bash
# 1. Harvest the last week with content.
python scripts/extract_run_fixtures.py \
    --since 7d \
    --include-content \
    --output ./candidates/

# 2. Pick a representative subset by hand (~50 calls).
# 3. Hand-label each one's expected counts into labels.json.
# 4. Move the picked fixtures + labels.json into your corpus dir.

# 5. Run the harness to confirm everything parses.
python scripts/validate_attribution.py --corpus ./my-corpus/
```

### Reproducing a single run offline

```bash
# Find the run id you care about.
sqlite3 ~/.inkfoot/runs.db \
    "SELECT id FROM runs WHERE task = 'customer-support-triage'
     ORDER BY started_at DESC LIMIT 1;"

# Extract a wide window so you definitely get the run's events.
python scripts/extract_run_fixtures.py \
    --since 7d \
    --include-content \
    --output ./debug/

# Find this run's fixtures.
ls ./debug/ | grep <run-id>
```
