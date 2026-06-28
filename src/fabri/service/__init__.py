"""B7 -- self-contained, embeddable fabri service (``fabri serve``).

Package the service pattern so a *non-Python* host can start a fabri instance,
submit a task, stream events, and read results + cost -- with no fabri Python
imports on the host side. O2 (in-process streaming) is not built, so events are
streamed by *tailing the JSONL trace* the agent loop already writes, not by
modifying the agent loop.

Pieces (each its own module so a host can reuse just what it needs):

- :mod:`.binding`  -- one immutable template config + per-run overrides ->
  a bound run yaml (reuses :func:`fabri.config._deep_merge`). Multi-tenancy seam.
- :mod:`.launcher` -- spawn the agent as a fresh-home subprocess (the same
  ``fabri run`` CLI); never re-implements the loop.
- :mod:`.tailer`   -- follow the run's trace and yield parsed
  :mod:`fabri.events` events live; ``extract_cost`` surfaces the ``usage`` event.
- :mod:`.sync`     -- ``sync_in`` / ``sync_out`` hooks mirroring
  :class:`fabri.sandbox.Sandbox`, with a no-op default.
- :mod:`.service`  -- :class:`FabriService` orchestrating the above, plus a
  stdio JSON-lines transport.
- :mod:`.http_server` -- a minimal stdlib HTTP transport (POST to submit, SSE
  GET to stream); the ``fabri serve`` command's backend.
"""
from fabri.service.binding import bind_run_config, merge_overrides
from fabri.service.launcher import RunHandle, build_run_command, launch_run
from fabri.service.service import FabriService, serve_stdio
from fabri.service.sync import FileSyncHook, NoOpSyncHook
from fabri.service.tailer import extract_cost, run_trace_path, tail_events

__all__ = [
    "FabriService",
    "FileSyncHook",
    "NoOpSyncHook",
    "RunHandle",
    "bind_run_config",
    "build_run_command",
    "extract_cost",
    "launch_run",
    "merge_overrides",
    "run_trace_path",
    "serve_stdio",
    "tail_events",
]
