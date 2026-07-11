"""External observability export for fabri's JSONL trace spine (Track X).

Off by default: `export_trace` is a no-op unless an OTLP endpoint is configured.
The OTel libraries are an optional extra — `pip install 'fabri[otel]'`.
"""
from fabri.observability.otel import OtelConfig, export_trace

__all__ = ["OtelConfig", "export_trace"]
