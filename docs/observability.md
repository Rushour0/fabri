# Observability — exporting fabri traces to OpenTelemetry / Langfuse

Fabri's homegrown JSONL trace spine (`.fabri/traces/<session_id>.jsonl`) is the
single source of truth for what a run did. **Observability is a thin,
off-by-default export shim on top of it** — it maps the trace to an OpenTelemetry
span tree and ships it over OTLP to any backend (Langfuse, Honeycomb, Datadog,
Grafana Tempo, Jaeger, …). When no endpoint is configured, behaviour is
byte-identical to no export.

> **Status (v0.10.2):** the exporter module (`fabri.observability.export_trace`)
> and its config surface ship and work from the library. It is **not yet wired
> into the CLI/run loop** — there is no `fabri traces export` verb or automatic
> end-of-run fire yet (that's the B2/B3 follow-up in
> `docs/design/memory-observability-plan.md`). Today you call `export_trace`
> yourself; see below.

## Install

The OpenTelemetry libraries live behind an optional extra:

```bash
pip install 'fabri[otel]'
```

That pulls `opentelemetry-sdk` + the HTTP OTLP exporter (the default) and the
gRPC exporter (for `otlp_protocol: grpc`).

## Configure

Add an `observability:` block to your agent config (all keys optional; the block
is inert until `otlp_endpoint` is set):

```yaml
observability:
  otlp_endpoint: https://cloud.langfuse.com/api/public/otel/v1/traces
  otlp_protocol: http            # "http" (default) or "grpc"
  otlp_headers:                  # backend auth / routing headers
    Authorization: "Basic <base64 of pk:sk>"
  otlp_insecure: false           # allow plaintext (gRPC only)
  service_name: fabri
```

Every field can also be set from the environment (useful for containers / CI —
they override the yaml at load time):

| Env var | Overrides |
|---|---|
| `FABRI_OTLP_ENDPOINT` | `observability.otlp_endpoint` |
| `FABRI_OTLP_PROTOCOL` | `observability.otlp_protocol` (`http`\|`grpc`) |
| `FABRI_OTLP_INSECURE` | `observability.otlp_insecure` (`1`/`true`/`yes`/`on`) |
| `FABRI_OTLP_HEADERS` | `observability.otlp_headers` (`k=v,k2=v2`) |

## Export a trace

```python
from fabri.config import load_config
from fabri.observability import OtelConfig, export_trace

config = load_config("agent.yaml")           # picks up the observability block + env
export_trace(session_id, OtelConfig.from_config(config))
# returns True if it exported, False if disabled (no otlp_endpoint) or nothing to send
```

`export_trace` reads the finished trace, walks its events into spans, and
force-flushes over OTLP:

- `fabri.agent_run` (root span) → `fabri.step` spans → `tool.<name>` spans.
- Thoughts, narration, the M3 `retrieval` event, errors, and cost/usage ride as
  span events / attributes — they export for free because the exporter just
  consumes whatever is in the trace.
- Sub-agent traces nest best-effort (parsed from a spawn tool-call's result
  payload); a child that can't be resolved is skipped.

## Recipe: Langfuse

Langfuse ingests OTLP directly — no Langfuse SDK needed.

```yaml
observability:
  otlp_endpoint: https://cloud.langfuse.com/api/public/otel/v1/traces  # or your self-host URL
  otlp_protocol: http
  otlp_headers:
    Authorization: "Basic <base64(public_key:secret_key)>"
  service_name: my-agent
```

Generate the header value with `echo -n "pk-lf-...:sk-lf-..." | base64`, or set it
out-of-band: `export FABRI_OTLP_HEADERS="Authorization=Basic <b64>"`.

## Recipe: generic OTLP (Honeycomb / Tempo / Jaeger / OTel Collector)

```yaml
observability:
  otlp_endpoint: http://localhost:4318/v1/traces   # HTTP; use :4317 for gRPC
  otlp_protocol: http                              # or grpc
  otlp_headers:
    x-honeycomb-team: "<api-key>"                  # backend-specific; omit for a local collector
  service_name: my-agent
```

For a local collector over plaintext gRPC, set `otlp_protocol: grpc` and
`otlp_insecure: true`.

## See also

- `docs/design/memory-observability-plan.md` — the full X1 design (event→span
  mapping, the pending CLI wiring, sub-agent nesting limits).
- `docs/using-fabri-well.md` §"Reading what memory did" — the `retrieval` trace
  event you'll see exported as span attributes.
