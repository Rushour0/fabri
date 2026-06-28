"""B7 -- the embeddable :class:`FabriService`.

Ties the pieces together: one immutable template config + per-run overrides ->
a bound run config (:mod:`.binding`) -> a fresh-home agent subprocess
(:mod:`.launcher`) -> a live event stream tailed from the trace (:mod:`.tailer`)
-> a result envelope with the run's cost surface. A host (Python or, via the
HTTP transport, any language) does:

    svc = FabriService(template_config="agent.yaml")
    session_id = svc.submit("do the thing", overrides={"llm": {"model": "..."}})
    for event in svc.stream(session_id):
        ...                      # live events.py vocabulary
    result = svc.result(session_id)   # {..., "cost": {total_cost_usd: ...}}

The service owns no agent logic -- it's pure orchestration over the existing
CLI + trace seams, so behaviour is identical to running ``fabri run`` by hand.
"""
from __future__ import annotations

import json
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from fabri.service.binding import bind_run_config
from fabri.service.launcher import RunHandle, launch_run
from fabri.service.sync import FileSyncHook, NoOpSyncHook
from fabri.service.tailer import extract_cost, tail_events

# A host may swap how the agent is launched (tests point this at a fake script).
# Signature: (task, config_path, session_id, fabri_home) -> argv.
CommandBuilder = Callable[[str, Path, str, Path], Sequence[str]]


class FabriService:
    """Self-contained instance that spawns agents from one template config.

    Args:
        template_config: path to the immutable base ``agent.yaml`` (or ``None``
            to inherit framework defaults). Per-run ``overrides`` deep-merge onto
            it.
        home_root: parent dir under which each run gets its own
            ``<home_root>/<session_id>`` ``FABRI_HOME``. Defaults to a fresh
            temp dir so runs never collide.
        sync_hook: optional :class:`FileSyncHook` invoked around each run to
            ferry workspace state. Defaults to a no-op.
        command_builder: optional override for the launch argv (tests / custom
            runtimes). ``None`` uses the standard ``fabri run`` command.
    """

    def __init__(
        self,
        template_config: str | Path | None = None,
        *,
        home_root: str | Path | None = None,
        sync_hook: FileSyncHook | None = None,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self.template_config = template_config
        self.home_root = (
            Path(home_root)
            if home_root is not None
            else Path(tempfile.mkdtemp(prefix="fabri-serve-"))
        )
        self.home_root.mkdir(parents=True, exist_ok=True)
        self.sync_hook: FileSyncHook = sync_hook or NoOpSyncHook()
        self.command_builder = command_builder
        self._runs: dict[str, RunHandle] = {}

    def submit(self, task: str, overrides: dict | None = None) -> str:
        """Bind a per-run config, launch the agent, return its ``session_id``."""
        session_id = str(uuid.uuid4())
        run_home = (self.home_root / session_id).resolve()
        run_home.mkdir(parents=True, exist_ok=True)
        self.sync_hook.sync_in(session_id, run_home)

        config_path = bind_run_config(
            self.template_config, overrides, run_home / "run.yaml"
        )
        command = None
        if self.command_builder is not None:
            command = self.command_builder(task, config_path, session_id, run_home)

        handle = launch_run(
            task,
            config_path=config_path,
            fabri_home=run_home,
            session_id=session_id,
            command=command,
        )
        self._runs[session_id] = handle
        return session_id

    def _handle(self, session_id: str) -> RunHandle:
        handle = self._runs.get(session_id)
        if handle is None:
            raise KeyError(f"unknown session_id {session_id!r}")
        return handle

    def stream(self, session_id: str, *, timeout: float | None = None) -> Iterator[dict]:
        """Yield the run's trace events live until it ends."""
        handle = self._handle(session_id)
        yield from tail_events(
            handle.trace_path, is_running=handle.is_running, timeout=timeout
        )

    def result(self, session_id: str, *, timeout: float | None = None) -> dict:
        """Block for the run and return its result envelope plus a cost surface.

        Reads cost from the trace's ``usage`` event (transport-agnostic, present
        even if the child's stdout was lost) and merges the agent's stdout
        envelope (``outcome`` / ``final_text`` / ``structured_output``).
        """
        handle = self._handle(session_id)
        envelope = handle.result(timeout=timeout)
        # Read the completed trace under the run's own home for the cost surface.
        events = _read_trace_at(handle.trace_path)
        cost = extract_cost(events)
        self.sync_hook.sync_out(session_id, handle.fabri_home, [])
        return {
            "session_id": session_id,
            "success": envelope.get("success"),
            "outcome": envelope.get("outcome"),
            "final_text": envelope.get("final_text"),
            "structured_output": envelope.get("structured_output"),
            "usage": envelope.get("usage"),
            "cost": cost,
            "error": envelope.get("error"),
        }

    def close(self) -> None:
        """Terminate any still-running children (best effort)."""
        for handle in self._runs.values():
            handle.terminate()


def _read_trace_at(path: Path) -> list[dict]:
    """Read a completed trace by absolute path (the run may live under a
    different ``FABRI_HOME`` than this process). Skips malformed lines, mirroring
    :func:`fabri.orchestrator.traces.read_trace`."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def serve_stdio(
    service: FabriService,
    *,
    stdin=None,
    stdout=None,
) -> None:
    """Stdio JSON-lines transport: one request object per input line.

    Each input line is a JSON object ``{"task": ..., "overrides": {...}?}``. For
    each, the service emits one JSON line per trace event
    (``{"session_id", "event": {...}}``) followed by a final result line
    (``{"session_id", "result": {...}}``). A non-Python host drives the whole
    run -- submit, stream, cost -- over stdin/stdout with no fabri imports.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def _write(obj: dict) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            _write({"error": f"invalid request JSON: {e}"})
            continue
        task = req.get("task")
        if not task:
            _write({"error": "request missing required field 'task'"})
            continue
        session_id = service.submit(task, req.get("overrides"))
        _write({"session_id": session_id, "status": "submitted"})
        for event in service.stream(session_id):
            _write({"session_id": session_id, "event": event})
        _write({"session_id": session_id, "result": service.result(session_id)})
