# OpenTelemetry Integration

Inkfoot speaks the OpenTelemetry GenAI semantic conventions in both
directions:

* **Ingest** — point an OTel collector at Inkfoot's local listener
  and your existing `gen_ai.*` spans become Inkfoot events. No SDK
  shim required; the events flow into the same SQLite store the
  Pattern A path uses.
* **Export** — Inkfoot mirrors every `llm_call` event as an OTLP
  span and every smell / outcome as an OTLP log to any collector
  endpoint you give it.

The implementation lives under `inkfoot.otel.*` and depends only
on the Python standard library. No `opentelemetry-sdk` install is
required for either direction; Inkfoot speaks OTLP/JSON over plain
HTTP.

## Mapping table

The mapping below is the contract between the OTel GenAI
conventions and Inkfoot's 14-field Causal Token Ledger. It is the
source of truth for both the ingest and the export paths.

| OTel GenAI attribute | Inkfoot field |
|---|---|
| `gen_ai.system` | `NeutralCall.provider` |
| `gen_ai.request.model` | `NeutralCall.model` |
| `gen_ai.usage.input_tokens` | Sum of the 13 input-side ledger fields (11 structural causes + 2 cache overlays) |
| `gen_ai.usage.output_tokens` | `ledger.output_tokens` |
| `gen_ai.response.id` | `event_id` (export) / dedup key (ingest) |
| `gen_ai.operation.name` | `"chat"` |
| `inkfoot.cause.system_static_tokens` | `ledger.system_static_tokens` |
| `inkfoot.cause.system_dynamic_tokens` | `ledger.system_dynamic_tokens` |
| `inkfoot.cause.user_input_tokens` | `ledger.user_input_tokens` |
| `inkfoot.cause.tool_schema_tokens` | `ledger.tool_schema_tokens` |
| `inkfoot.cause.tool_result_tokens` | `ledger.tool_result_tokens` |
| `inkfoot.cause.retrieved_context_tokens` | `ledger.retrieved_context_tokens` |
| `inkfoot.cause.memory_tokens` | `ledger.memory_tokens` |
| `inkfoot.cause.retry_overhead_tokens` | `ledger.retry_overhead_tokens` |
| `inkfoot.cause.summariser_tokens` | `ledger.summariser_tokens` |
| `inkfoot.cause.reasoning_tokens` | `ledger.reasoning_tokens` |
| `inkfoot.cause.guardrail_tokens` | `ledger.guardrail_tokens` |
| `inkfoot.cause.cache_creation_tokens` | `ledger.cache_creation_tokens` |
| `inkfoot.cause.cache_read_tokens` | `ledger.cache_read_tokens` |
| `inkfoot.estimation_flags` | `NeutralCall.estimation_flags` (CSV) |
| `inkfoot.estimated_nanodollars` | `NeutralCall.estimated_nanodollars` |
| `inkfoot.run_id` | The run the event belongs to |
| `inkfoot.event_kind` | `"llm_call"` / `"smell"` / `"outcome"` |
| `inkfoot.sequence` | Within-run sequence number |

The mapping is version-pinned: `OTEL_GENAI_CONVENTIONS_VERSION =
"1.27.0"` in `inkfoot.otel.conventions`. Bumping the spec version
requires an explicit edit; CI catches the literal so an
auto-upgrade can't silently change the wire format.

## Ingest — point your collector at Inkfoot

Enable the listener by passing `otel_ingest_port` to
`inkfoot.instrument()`:

```python
import inkfoot

inkfoot.instrument(
    otel_ingest_port=4318,
    otel_ingest_host="127.0.0.1",  # optional; defaults to loopback
)
```

The receiver accepts `POST /v1/traces` requests carrying OTLP/JSON.
For an OTel collector, configure the OTLP HTTP exporter with the
JSON encoder:

```yaml
# collector.yaml
exporters:
  otlphttp/inkfoot:
    endpoint: http://localhost:4318
    encoding: json   # Inkfoot ingest is JSON-only

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/inkfoot]
```

### What happens to a span

1. Inkfoot decodes the OTLP/JSON attributes.
2. The 11 structural causes + 2 cache overlays + output are
   reassembled into a `CausalTokenLedger`.
3. The span lands as an `llm_call` event in storage, under a run
   keyed on the OTLP `trace_id`. When the span carries an
   `inkfoot.run_id` attribute, that wins; otherwise Inkfoot
   synthesises a run with task `"otel-ingest"` so cross-span trace
   grouping survives.
4. Spans the SDK shim has already captured (same
   `(span_id, gen_ai.response.id)` pair) are silently dropped.

### Reading the ingest response

Every `POST /v1/traces` response carries an `X-Inkfoot-Stats`
header summarising the request:

```
X-Inkfoot-Stats: accepted=N;duplicates=N;rejected=N;skipped_non_genai=N
```

The four counters correspond to:

| Key | Meaning |
|---|---|
| `accepted` | Spans translated and persisted as `llm_call` events. |
| `duplicates` | Spans dropped by dedup (already seen by the SDK shim). |
| `rejected` | Spans where translation or persistence raised. |
| `skipped_non_genai` | Spans that carry no `gen_ai.*` attribute. The receiver intentionally drops them rather than producing `provider="unknown"` storage rows — useful when a collector pipeline forwards its full trace export rather than only the GenAI spans. |

The same four counters are also tracked on the receiver's
`stats` attribute as a process-wide rolling sum
(`OTLPHTTPReceiver.stats`), so an operator with shell access can
read them without parsing HTTP responses.

### What ingest doesn't accept

* `Content-Type: application/x-protobuf` returns `415` with a
  remediation hint. Reconfigure the collector to use the OTLP
  JSON encoder.
* Anything other than `POST /v1/traces` returns `404`.

## Export — forward Inkfoot events to your collector

Enable export by passing the collector base URL:

```python
import inkfoot

inkfoot.instrument(
    otel_export_endpoint="http://otel-collector.local:4318",
)
```

Every `llm_call` event becomes one OTLP span (`POST /v1/traces`)
with the full `inkfoot.cause.*` breakdown attached as attributes.
`smell` and `outcome` events forward as OTLP logs
(`POST /v1/logs`).

The exporter runs on a background thread with a bounded queue
(1024 events by default). Under sustained overload Inkfoot drops
events with a WARN log rather than back-pressuring the agent's
hot path. A failing collector logs WARN and the agent keeps
running — Inkfoot never blocks on the network.

### Tuning

Defaults are tuned for "fits a single OTLP request without
splitting":

| Constant | Default |
|---|---|
| `DEFAULT_BATCH_SIZE` | 64 events |
| `DEFAULT_BATCH_INTERVAL_S` | 1.0 second |
| `DEFAULT_QUEUE_CAPACITY` | 1024 events |
| `DEFAULT_EXPORT_TIMEOUT_S` | 5.0 seconds |

For higher-throughput workloads, build an `OTLPExporter`
directly:

```python
from inkfoot.otel.export import ExportTransport, OTLPExporter

transport = ExportTransport(endpoint="...", timeout=10.0)
exporter = OTLPExporter(
    transport=transport,
    batch_size=256,
    queue_capacity=4096,
)
```

## End-to-end OTel pipeline

* Ingest + export can both be enabled simultaneously. An ingest
  span goes straight into storage; the export tap fires on the
  shim's own `insert_event` calls, **not** on ingested spans, so
  you don't accidentally re-export your own pipeline back into
  itself.
* The `inkfoot.run_id` / `inkfoot.sequence` extension attributes
  preserve run grouping when an Inkfoot-instrumented service
  exports to a downstream Inkfoot ingest.

See the [Honeycomb recipe](../recipes/otel-honeycomb.md) for a
copy-pasteable end-to-end example.

## Working alongside framework adapters

The OTel export path is adapter-agnostic — it taps the event
stream the shim and adapters write to, so per-node and per-tool
metadata flows through to your collector regardless of how you
instrumented the agent:

- [LangGraph](../frameworks/langgraph.md) — `inkfoot.run_id` and
  the active `node_name` ride through on every span, so a
  Honeycomb / Grafana query can slice cost by LangGraph node
  without any extra wiring.
- [OpenAI Agents SDK](../frameworks/openai-agents.md) — per-tool
  attribution exports with the tool name in `node_name`. Group
  by `inkfoot.cause.tool_result_tokens` in your backend to see
  which tool's results dominate the input bill.
- [Anthropic Agent SDK](../frameworks/anthropic-agent.md) — same
  shape as the OpenAI Agents adapter; spans inherit the
  per-tool name.
- [Raw provider SDK](../frameworks/raw-sdk.md) — pair OTel export
  with `inkfoot.tag_node(...)` to slice spans by a
  manually-tagged phase. Without `tag_node`, the spans still
  export but they all land under the same un-tagged bucket.

The 14 ledger fields land under the
[`inkfoot.cause.*` attribute namespace](#mapping-table) on every
exported span regardless of which adapter scoped the run, so
downstream dashboards built around the mapping table work
identically across frameworks.
