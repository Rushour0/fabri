"""Export fabri's homegrown JSONL trace to any OpenTelemetry OTLP backend (X1).

Fabri owns its events — the JSONL spine is the single source of truth. This is a
thin, **off-by-default export shim** on top of it, not a bespoke protocol and not
a Langfuse-SDK dependency. Langfuse, Honeycomb, Datadog, Grafana Tempo, Jaeger,
etc. are all reached as just another OTLP endpoint + headers target. When no
endpoint is configured, behaviour is byte-identical to no export.

v1 is a **post-hoc batch exporter**: it reads a finished trace, walks the events
into an OTel span tree (run → step → tool spans, with thoughts/narration/
retrieval as span events), and force-flushes over OTLP. Because it consumes
whatever is in the trace, M3's `retrieval` event and the cost/usage events export
for free. Sub-agent traces nest best-effort — parsed from a spawn tool-call's
result payload; a child we can't resolve is silently skipped (v1 accepts that).

    from fabri.observability import OtelConfig, export_trace
    export_trace(session_id, OtelConfig.from_config(config))

The OTel libraries live behind the optional `otel` extra:
`pip install 'fabri[otel]'`. See docs/observability.md for the Langfuse and
generic-OTLP recipes.

Not to be confused with the `otel` *ingest* adapter
(`fabri.ingest.adapters.builtins:otel_adapter`), which reads OTel-shaped
EXTERNAL logs INTO memory. This module exports fabri's OWN traces OUT.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from fabri.orchestrator.traces import read_trace

logger = logging.getLogger("fabri")

# Guard against a pathological or cyclic spawn graph blowing the stack.
_MAX_SUBAGENT_DEPTH = 5

_INSTALL_HINT = (
    "OpenTelemetry export needs the optional 'otel' extra. "
    "Install it with:  pip install 'fabri[otel]'"
)

# Trace event types that map to their own child span vs. a point-in-time span
# event on the enclosing span. Everything not named a span here is an event.
_STEP_START = "step_started"
_STEP_END = "step_finished"
_TOOL_START = "tool_started"
_TOOL_END = "tool_call"
_USAGE_TYPES = {"usage", "post_run_usage"}
_OUTCOME_TYPES = {"final", "failed", "incomplete"}


@dataclass
class OtelConfig:
    """Resolved OTLP export settings. `endpoint` unset => export is a no-op."""

    endpoint: str | None = None          # OTLP endpoint URL; None disables export
    protocol: str = "http"               # "http" (OTLP/HTTP protobuf) or "grpc"
    headers: dict[str, str] = field(default_factory=dict)  # e.g. Langfuse auth
    insecure: bool = False               # allow plaintext (gRPC only)
    service_name: str = "fabri"

    @classmethod
    def from_config(cls, config: dict) -> "OtelConfig":
        obs = (config or {}).get("observability") or {}
        return cls(
            endpoint=obs.get("otlp_endpoint") or None,
            protocol=(obs.get("otlp_protocol") or "http").lower(),
            headers=dict(obs.get("otlp_headers") or {}),
            insecure=bool(obs.get("otlp_insecure", False)),
            service_name=obs.get("service_name") or "fabri",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)


def export_trace(session_id: str, otel_cfg: OtelConfig) -> bool:
    """Read the finished trace for `session_id`, map it to an OTel span tree, and
    flush it over OTLP. Returns True if a trace was exported, False if export is
    disabled (no endpoint) or the trace is empty/has no `start` event.

    Raises ImportError (with a `pip install fabri[otel]` hint) when an endpoint is
    configured but the OTel libraries are missing — a misconfiguration worth
    surfacing loudly, unlike the best-effort auto-export path in the CLI which
    swallows it."""
    if not otel_cfg.enabled:
        return False

    events = read_trace(session_id)
    if not events or not any(e.get("type") == "start" for e in events):
        logger.debug("otel export: trace %s empty or has no start event; skipped", session_id)
        return False

    provider = _build_provider(otel_cfg)
    tracer = provider.get_tracer("fabri")
    try:
        _emit_trace_spans(events, tracer, session_id)
    finally:
        # Batch processor flush is what actually ships the spans over OTLP.
        provider.force_flush()
        provider.shutdown()
    return True


def _build_provider(otel_cfg: OtelConfig):
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:  # pragma: no cover - exercised via the CLI error path
        raise ImportError(_INSTALL_HINT) from e

    provider = TracerProvider(resource=Resource.create({"service.name": otel_cfg.service_name}))
    provider.add_span_processor(BatchSpanProcessor(_build_exporter(otel_cfg)))
    return provider


def _build_exporter(otel_cfg: OtelConfig):
    """Lazy-import the exporter matching the configured protocol. HTTP is the
    default (Langfuse-native, lighter dep); gRPC needs its own extra."""
    headers = otel_cfg.headers or None
    if otel_cfg.protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as e:
            raise ImportError(
                "otlp_protocol='grpc' needs the gRPC exporter: "
                "pip install opentelemetry-exporter-otlp-proto-grpc"
            ) from e
        return OTLPSpanExporter(endpoint=otel_cfg.endpoint, headers=headers, insecure=otel_cfg.insecure)
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as e:  # pragma: no cover - exercised via the CLI error path
        raise ImportError(_INSTALL_HINT) from e
    return OTLPSpanExporter(endpoint=otel_cfg.endpoint, headers=headers)


def _ns(ts) -> int:
    """Trace `ts` is epoch seconds (float); OTel spans want epoch nanoseconds."""
    return int(float(ts) * 1_000_000_000)


def _emit_trace_spans(events, tracer, session_id, parent_ctx=None, depth: int = 0) -> None:
    """Walk one session's events into a span tree on `tracer`.

    Factored out of `export_trace` so the mapping is testable against any tracer
    (e.g. an in-memory span exporter) without touching OTLP. `parent_ctx` nests
    this run's root span under a caller's span (sub-agent linking)."""
    from opentelemetry import trace as ot_trace

    start_ev = next((e for e in events if e.get("type") == "start"), None)
    if start_ev is None:
        return
    first_ts = events[0].get("ts")
    last_ts = events[-1].get("ts", first_ts)

    root = tracer.start_span("fabri.agent_run", context=parent_ctx, start_time=_ns(first_ts))
    root.set_attribute("fabri.session_id", session_id)
    if start_ev.get("task"):
        root.set_attribute("fabri.task", str(start_ev["task"])[:1000])
    root_ctx = ot_trace.set_span_in_context(root)

    current_step = None
    current_step_ctx = root_ctx
    # Keyed by the event's `call_index` (unique per tool call), NOT the tool
    # name: a parallel_group fan-out runs several calls to the SAME tool name
    # concurrently and completes them out of order, so name-keying would close
    # the wrong span. `call_index` can be 0, so fall back to name only when it
    # is genuinely absent (older traces / synthesized events).
    open_tools: dict[object, object] = {}

    def _enclosing():
        return current_step if current_step is not None else root

    for ev in events:
        etype = ev.get("type")
        ts = _ns(ev.get("ts", first_ts))

        if etype == _STEP_START:
            if current_step is not None:
                current_step.end(end_time=ts)
            current_step = tracer.start_span("fabri.step", context=root_ctx, start_time=ts)
            if ev.get("step") is not None:
                current_step.set_attribute("fabri.step", ev["step"])
            current_step_ctx = ot_trace.set_span_in_context(current_step)

        elif etype == _STEP_END:
            if current_step is not None:
                current_step.end(end_time=ts)
                current_step, current_step_ctx = None, root_ctx

        elif etype == _TOOL_START:
            name = ev.get("tool") or ev.get("name") or "tool"
            ci = ev.get("call_index")
            key = ci if ci is not None else name
            span = tracer.start_span(f"tool.{name}", context=current_step_ctx, start_time=ts)
            span.set_attribute("fabri.tool.name", name)
            open_tools[key] = span

        elif etype == _TOOL_END:
            name = ev.get("name") or ev.get("tool") or "tool"
            ci = ev.get("call_index")
            key = ci if ci is not None else name
            span = open_tools.pop(key, None)
            if span is None:
                # No matching tool_started (e.g. a scripted path) — synthesize one.
                span = tracer.start_span(f"tool.{name}", context=current_step_ctx, start_time=ts)
                span.set_attribute("fabri.tool.name", name)
            _set_tool_attrs(span, ev)
            child_sid = _child_session_id(ev)
            if child_sid and depth < _MAX_SUBAGENT_DEPTH:
                child_events = read_trace(child_sid)
                if child_events:
                    _emit_trace_spans(
                        child_events, tracer, child_sid,
                        parent_ctx=ot_trace.set_span_in_context(span), depth=depth + 1,
                    )
                else:
                    span.set_attribute("fabri.subagent.unresolved", True)
            span.end(end_time=ts)

        elif etype in _USAGE_TYPES:
            _set_usage_attrs(root, ev)

        elif etype in _OUTCOME_TYPES:
            root.set_attribute("fabri.outcome", ev.get("outcome") or etype)

        elif etype is not None:
            # thought / narration / retrieval / error / ask_user / discrepancy /
            # structured_output / plan_* — a point-in-time event on its span.
            _enclosing().add_event(etype, attributes=_event_attrs(ev), timestamp=ts)

    end_ns = _ns(last_ts)
    for span in open_tools.values():
        span.end(end_time=end_ns)
    if current_step is not None:
        current_step.end(end_time=end_ns)
    root.end(end_time=end_ns)


def _child_session_id(tool_call_ev: dict) -> str | None:
    """Best-effort extraction of a spawned sub-agent's session id from a spawn
    tool-call result (the runner returns `{session_id, trace_path}`). Accepts a
    dict or a JSON string; returns None for any non-spawn / unparseable result."""
    result = tool_call_ev.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None
    if isinstance(result, dict):
        sid = result.get("session_id") or result.get("sid")
        if isinstance(sid, str) and sid:
            return sid
    return None


def _set_tool_attrs(span, ev: dict) -> None:
    result = ev.get("result")
    ok = None
    if isinstance(result, dict):
        ok = result.get("ok")
        if "error" in result and result.get("error"):
            span.set_attribute("fabri.tool.error", str(result["error"])[:500])
    if ok is not None:
        span.set_attribute("fabri.tool.ok", bool(ok))
    if ev.get("parallel_group"):
        span.set_attribute("fabri.tool.parallel_group", str(ev["parallel_group"]))


def _set_usage_attrs(root, ev: dict) -> None:
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "cost_usd"):
        val = ev.get(key)
        if val is not None:
            # post_run_usage adds to totals; expose both under a source-tagged key.
            attr = f"fabri.usage.{key}" if ev.get("type") == "usage" else f"fabri.post_usage.{key}"
            root.set_attribute(attr, val)


def _event_attrs(ev: dict) -> dict:
    """Flatten one trace event to OTel-safe span-event attributes (str/num/bool;
    nested structures JSON-stringified and length-capped)."""
    out: dict[str, object] = {}
    for k, v in ev.items():
        if k in ("type", "ts"):
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v if not isinstance(v, str) else v[:1000]
        else:
            try:
                out[k] = json.dumps(v)[:2000]
            except (TypeError, ValueError):
                out[k] = str(v)[:2000]
    return out
