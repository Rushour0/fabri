"""Unit tests for the X1 OTel export shim (config surface + span mapping).

The exporter is off-by-default and not yet wired into the CLI, but the two
things that CAN bite a user today are covered here: the FABRI_OTLP_* env
overrides (documented in config.py / docs/observability.md) and the tool-span
keying under a parallel_group fan-out.
"""
import pytest

from fabri.config import load_config


# ---- FABRI_OTLP_* env overrides -------------------------------------------

def test_otlp_env_overrides_populate_observability(monkeypatch):
    monkeypatch.setenv("FABRI_OTLP_ENDPOINT", "https://collector.example/otel")
    monkeypatch.setenv("FABRI_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("FABRI_OTLP_INSECURE", "true")
    monkeypatch.setenv("FABRI_OTLP_HEADERS", "Authorization=Basic abc, X-Scope=team1")

    obs = load_config(None)["observability"]

    assert obs["otlp_endpoint"] == "https://collector.example/otel"
    assert obs["otlp_protocol"] == "grpc"
    assert obs["otlp_insecure"] is True
    assert obs["otlp_headers"] == {"Authorization": "Basic abc", "X-Scope": "team1"}


def test_no_otlp_env_leaves_defaults(monkeypatch):
    for k in ("FABRI_OTLP_ENDPOINT", "FABRI_OTLP_PROTOCOL",
              "FABRI_OTLP_INSECURE", "FABRI_OTLP_HEADERS"):
        monkeypatch.delenv(k, raising=False)

    obs = load_config(None)["observability"]

    assert obs["otlp_endpoint"] is None            # inert: nothing exports
    assert obs["otlp_headers"] == {}


# ---- tool-span keying under a parallel fan-out ----------------------------

def test_tool_spans_keyed_by_call_index_not_name(monkeypatch):
    """Two concurrent spawn_subagent calls (same tool name) that complete out
    of order must map to their OWN spans. Name-keying would close the wrong
    span and orphan the other; call_index keying is correct."""
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from fabri.observability import otel as otelmod

    # call 0: [100, 110]; call 1: [101, 105] — completes BEFORE call 0.
    events = [
        {"ts": 100.0, "type": "start", "task": "t"},
        {"ts": 100.0, "type": "step_started", "step": 1},
        {"ts": 100.0, "type": "tool_started", "name": "spawn_subagent", "call_index": 0},
        {"ts": 101.0, "type": "tool_started", "name": "spawn_subagent", "call_index": 1},
        {"ts": 105.0, "type": "tool_call", "name": "spawn_subagent", "call_index": 1, "result": {"ok": True}},
        {"ts": 110.0, "type": "tool_call", "name": "spawn_subagent", "call_index": 0, "result": {"ok": True}},
        {"ts": 111.0, "type": "step_finished", "step": 1},
        {"ts": 111.0, "type": "run_finished", "outcome": "success"},
    ]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    otelmod._emit_trace_spans(events, tracer, "sid-test")

    tool_spans = [s for s in exporter.get_finished_spans() if s.name == "tool.spawn_subagent"]
    assert len(tool_spans) == 2, "expected exactly two tool spans, one per call_index"

    windows = {
        (round(s.start_time / 1_000_000_000), round(s.end_time / 1_000_000_000))
        for s in tool_spans
    }
    assert windows == {(100, 110), (101, 105)}, (
        f"tool spans have wrong start/end windows: {windows} — "
        "name-keying corrupts these under a parallel fan-out"
    )
