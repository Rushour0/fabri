"""The Improver — fabri's plug-and-play log-ingestion layer.

Point fabri at any log (app logs, CI output, OTel/OpenAI traces) and the
failures + successes in it mine into the same self-improving memory the agent
loop learns from. SDK-first: ``fabri.readlogs(...)`` / :class:`Improver`.
Adapters plug in three ways — the ``@fabri.adapter`` decorator, a declarative
config field-map, or a polyglot executable — plus pip-installed plugins via the
``fabri.adapters`` entry-point group.
"""
from fabri.ingest.adapters.base import Adapter, Session, final_event, start_event, tool_event
from fabri.ingest.api import readlogs
from fabri.ingest.improver import Improver, NoOpLLM
from fabri.ingest.registry import (
    UnknownAdapterError,
    adapter,
    get_adapter,
    list_adapters,
    register_adapter,
    resolve_adapter,
)
from fabri.ingest.sources import LogSource
from fabri.ingest.summary import IngestSummary


def _register_builtin_adapters() -> None:
    """Register the bundled adapters programmatically so they're always
    available (no reinstall needed for the entry-point group to take effect).
    Third-party adapters still arrive via ``fabri.adapters`` entry points."""
    from fabri.ingest.adapters.builtins import jsonl_adapter, otel_adapter, regex_adapter

    register_adapter("jsonl", jsonl_adapter)
    register_adapter("regex", regex_adapter)
    register_adapter("otel", otel_adapter)
    register_adapter("openai", otel_adapter)  # openai-response shape handled by the same mapper


_register_builtin_adapters()

__all__ = [
    "Adapter",
    "Improver",
    "IngestSummary",
    "LogSource",
    "NoOpLLM",
    "Session",
    "UnknownAdapterError",
    "adapter",
    "final_event",
    "get_adapter",
    "list_adapters",
    "readlogs",
    "register_adapter",
    "resolve_adapter",
    "start_event",
    "tool_event",
]
