# Recipe: Inkfoot + OpenTelemetry → Honeycomb

This recipe walks an existing app with OTel auto-instrumentation
all the way to Inkfoot-aware Honeycomb traces. Two complementary
flows show up:

1. **Ingest** — the app already exports OTel GenAI spans. Point
   the collector at Inkfoot and Inkfoot stores them locally
   alongside any SDK-shim events.
2. **Export** — Inkfoot mirrors its own events back out to
   Honeycomb so a single backend has the full picture (cost,
   smells, outcomes) plus the existing infrastructure spans.

## Prerequisites

* Python 3.10+.
* `inkfoot` ≥ 0.1 (the OTel hooks are available in this release).
* An OTel collector binary (the
  [contrib distro](https://github.com/open-telemetry/opentelemetry-collector-contrib)
  is fine).
* A Honeycomb API key.

This recipe shows the raw-SDK case for concreteness, but the
pipeline is adapter-agnostic — the same flow works for
[LangGraph](../frameworks/langgraph.md),
[OpenAI Agents SDK](../frameworks/openai-agents.md), and
[Anthropic Agent SDK](../frameworks/anthropic-agent.md). The
per-node / per-tool metadata each adapter sets rides through to
Honeycomb on the `node_name` attribute.

## 1. Configure the collector

Two pipelines: one drains your app's existing OTel exports into
Inkfoot for cost attribution; the other drains everything to
Honeycomb. The Inkfoot ingest currently accepts OTLP/JSON, so the
`encoding: json` knob is load-bearing.

```yaml
# collector.yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4319   # what your app's SDK ships to

processors:
  batch:
    timeout: 1s

exporters:
  otlphttp/inkfoot:
    endpoint: http://localhost:4318
    encoding: json
  otlp/honeycomb:
    endpoint: api.honeycomb.io:443
    headers:
      x-honeycomb-team: ${HONEYCOMB_API_KEY}

service:
  pipelines:
    traces/inkfoot:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/inkfoot]
    traces/honeycomb:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/honeycomb]
```

Run the collector:

```bash
HONEYCOMB_API_KEY=hcaik_... otelcol --config collector.yaml
```

## 2. Wire Inkfoot to ingest + re-export

```python
# app/bootstrap.py
import inkfoot

inkfoot.instrument(
    otel_ingest_port=4318,                       # accept the collector's forwards
    otel_export_endpoint="http://localhost:4319",  # mirror our events back to OTel
)
```

* `otel_ingest_port=4318` opens the local listener. Inkfoot now
  stores every GenAI span the collector forwards as an `llm_call`
  event, with the 11 structural causes + 2 cache overlays mapped
  per the [OTel mapping table](../concepts/otel.md#mapping-table).
* `otel_export_endpoint="http://localhost:4319"` (back to the
  collector's receiver) means Inkfoot's own events (smells,
  outcomes, plus any calls captured via the SDK shim) flow back
  through the collector to Honeycomb.

The two flows don't cycle: ingested spans bypass the export tap
by design — a span that came in from the collector won't be
re-exported out the same endpoint.

## 3. Run the app

Use the existing app as-is. The collector picks up its OTel
spans, Inkfoot ingests them, your reports populate, and Honeycomb
receives both the original spans **and** Inkfoot's enriched
events. Sample query in Honeycomb:

```
filter inkfoot.event_kind = "llm_call"
visualise heatmap of inkfoot.cause.tool_result_tokens by gen_ai.request.model
```

Smells show up as logs with severity WARN; outcomes show up as
logs with severity INFO. Pair them with the spans they reference
via `inkfoot.run_id`.

## 4. Sanity check the round-trip

```bash
inkfoot report --last 24h --group-by task
```

Should show a row for `otel-ingest` containing every span the
collector forwarded today (plus any natively-shimmed Pattern-A
runs). If the row is empty, the most likely culprits are:

* Collector pipeline misnamed (`exporters: [otlphttp/inkfoot]`).
* Collector exporting protobuf instead of JSON (JSON ingest is
  JSON-only).
* Inkfoot's ingest port already in use — pick a different port
  via `otel_ingest_port=...` and update the collector config to
  match.

## Cost note

Live re-export multiplies your Honeycomb event volume by roughly
one OTel span per `llm_call` plus one log per smell / outcome.
For high-throughput agents, raise `DEFAULT_BATCH_SIZE` or build
an `OTLPExporter` directly — see the
[concepts page](../concepts/otel.md#tuning).
