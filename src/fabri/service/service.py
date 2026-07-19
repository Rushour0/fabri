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
import logging
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from threading import Lock

from fabri.config import ConfigError, load_config
from fabri.events import EventType
from fabri.service.ask_user_listener import AskUserListener, append_trace_event
from fabri.service.auth import UserStore
from fabri.service.binding import bind_run_config, merge_overrides
from fabri.service.launcher import RunHandle, launch_run
from fabri.service.run_store import RunStore
from fabri.service.slack_notify import post_ask_user_question
from fabri.service.sync import FileSyncHook, NoOpSyncHook
from fabri.service.tailer import extract_cost, run_trace_path, tail_events

# Short base dir for per-run ask_user sockets. AF_UNIX paths are capped (~104
# bytes on macOS), so we deliberately avoid nesting them under a deep run home.
_ASK_SOCKET_DIR = Path(tempfile.gettempdir()) / "fabri-ask"
_LOG = logging.getLogger("fabri")

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
            ``<home_root>/<session_id>`` ``FABRI_HOME``. Defaults to
            ``${FABRI_HOME:-~/.fabri}/serve`` so history survives restarts.
            Set ``FABRI_EPHEMERAL=1`` to use a fresh temp dir instead (useful
            for isolated tests and CI).
        sync_hook: optional :class:`FileSyncHook` invoked around each run to
            ferry workspace state. Defaults to a no-op.
        command_builder: optional override for the launch argv (tests / custom
            runtimes). ``None`` uses the standard ``fabri run`` command.
        catalog: optional pre-installed roster entries keyed by their catalog
            reference. A submit with ``catalog_ref`` selects that entry's config.
    """

    def __init__(
        self,
        template_config: str | Path | None = None,
        *,
        home_root: str | Path | None = None,
        sync_hook: FileSyncHook | None = None,
        command_builder: CommandBuilder | None = None,
        catalog: Mapping[str, dict] | None = None,
    ) -> None:
        self.template_config = template_config
        self.catalog = catalog
        try:
            loaded_config = load_config(
                str(self.template_config) if self.template_config else None
            )
            self._slack_cfg = loaded_config.get("routing", {}).get("slack", {})
            self.auth_cfg = loaded_config.get("auth", {})
            self._default_agency = loaded_config.get("agent", {}).get("name", "default")
        except ConfigError:
            _LOG.warning("could not load Slack routing configuration", exc_info=True)
            self._slack_cfg = {}
            self.auth_cfg = {}
            self._default_agency = "default"
        self.home_root = (
            Path(home_root)
            if home_root is not None
            else Path(tempfile.mkdtemp(prefix="fabri-serve-"))
            if os.environ.get("FABRI_EPHEMERAL") == "1"
            else Path(os.environ.get("FABRI_HOME") or (Path.home() / ".fabri")) / "serve"
        )
        self.home_root.mkdir(parents=True, exist_ok=True)
        self.auth_enabled = bool(self.auth_cfg.get("enabled"))
        self.auth_secret: str | None = None
        self.user_store: UserStore | None = None
        if self.auth_enabled:
            secret_env = self.auth_cfg.get("session_secret_env", "FABRI_AUTH_SECRET")
            secret = os.environ.get(secret_env)
            if not secret:
                raise RuntimeError(
                    f"authentication is enabled; set the {secret_env} environment variable"
                )
            self.auth_secret = secret
            db_path = self.auth_cfg.get("db_path") or self.home_root / "auth.db"
            self.user_store = UserStore(db_path)
        self.sync_hook: FileSyncHook = sync_hook or NoOpSyncHook()
        self.command_builder = command_builder
        self._runs: dict[str, RunHandle] = {}
        # Per-run human-in-the-loop bridge for the ask_user tool.
        self._asks: dict[str, AskUserListener] = {}
        # Live per-session metadata (task + thread/fleet grouping + submit time)
        # so ``list_sessions`` can describe an in-flight run before it has a
        # terminal index line. Persisted copies live in ``index.jsonl``.
        self._meta: dict[str, dict] = {}
        # Sessions that already wrote a terminal index line, so a second
        # ``result()`` (SSE tail + a recovery ``/result`` fetch both call it)
        # doesn't double-record.
        self._finalized: set[str] = set()
        # Serialize appends to the on-disk session index (multiple run threads
        # may terminate concurrently under ThreadingHTTPServer).
        self._index_lock = Lock()
        self.run_store = RunStore(
            self.home_root / "runs.db",
            index_path=self._index_path,
            default_agency=self._default_agency,
        )

    @property
    def _index_path(self) -> Path:
        """Append-only session index; the persistence behind ``list_sessions``.

        One JSON line per lifecycle transition (``submit`` then a terminal
        ``result``/``cancel``). Lives beside the per-run homes so it survives a
        ``serve`` restart -- history is re-derived from here, not held in RAM.
        """
        return self.home_root / "index.jsonl"

    def _append_index(self, record: dict) -> None:
        record = {"ts": round(time.time(), 3), **record}
        line = json.dumps(record) + "\n"
        with self._index_lock:
            with self._index_path.open("a") as fh:
                fh.write(line)

    def submit(
        self,
        task: str,
        overrides: dict | None = None,
        *,
        thread_id: str | None = None,
        fleet_id: str | None = None,
        label: str | None = None,
        catalog_ref: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Bind a per-run config, launch the agent, return its ``session_id``.

        ``thread_id`` groups multi-turn runs (a conversation); ``fleet_id`` groups
        one fan-out batch; ``label`` is a human name for this run within a fleet
        (e.g. an account name). All are opaque tags recorded in the index so
        ``list_sessions`` / fleet roll-ups can reconstruct the grouping; the agent
        loop itself is unaware of them. When ``catalog_ref`` is supplied, its
        pre-installed catalog config replaces ``self.template_config`` for this run.
        """
        session_id = str(uuid.uuid4())
        run_home = (self.home_root / session_id).resolve()
        run_home.mkdir(parents=True, exist_ok=True)
        self.sync_hook.sync_in(session_id, run_home)

        template_config = self.template_config
        agency = self._default_agency
        if catalog_ref is not None:
            if self.catalog is None or catalog_ref not in self.catalog:
                raise KeyError(f"unknown catalog_ref {catalog_ref!r}")
            template_config = self.catalog[catalog_ref]["config"]
            agency = catalog_ref
        config_path = bind_run_config(template_config, overrides, run_home / "run.yaml")
        command = None
        if self.command_builder is not None:
            command = self.command_builder(task, config_path, session_id, run_home)

        # Start the ask_user bridge before launching the run, and point the
        # child at its socket via the env. The listener appends an `ask_user`
        # event to the run's own trace (same path the launcher will write/tail),
        # so a mid-run question reaches the browser over the existing SSE stream.
        trace_path = run_trace_path(run_home, session_id)
        listener = self._start_ask_listener(session_id, trace_path)

        handle = launch_run(
            task,
            config_path=config_path,
            fabri_home=run_home,
            session_id=session_id,
            command=command,
            env={"FABRI_ASK_USER_SOCKET": listener.socket_path},
        )
        self._runs[session_id] = handle
        meta = {
            "session_id": session_id,
            "task": task,
            "thread_id": thread_id,
            "fleet_id": fleet_id,
            "label": label,
            "started_ts": round(time.time(), 3),
            "agency": agency,
            "user_id": user_id,
        }
        self._meta[session_id] = meta
        self._append_index({"event": "submit", **meta})
        self.run_store.record_submit(
            session_id=session_id,
            agency=agency,
            task=task,
            submitted_at=meta["started_ts"],
            thread_id=thread_id,
            fleet_id=fleet_id,
            label=label,
            user_id=user_id,
        )
        return session_id

    def submit_fleet(
        self, items: list[dict], overrides: dict | None = None, *, user_id: str | None = None
    ) -> dict:
        """Fan one batch out to N runs sharing a ``fleet_id``.

        ``items`` is a list of ``{"task", "label"?, "overrides"?}``. Fleet-level
        ``overrides`` apply to every item, deep-merged under each item's own
        overrides (item wins). A fleet is nothing but a tagged group of ordinary
        runs -- the roll-up (:meth:`fleet_status`) sums their existing per-run
        cost surfaces; there is no new engine.
        """
        fleet_id = str(uuid.uuid4())
        base = overrides or {}
        sessions: list[dict] = []
        for it in items:
            task = it.get("task")
            if not task:
                raise ValueError("each fleet item requires a 'task'")
            item_overrides = merge_overrides(base, it.get("overrides"))
            sid = self.submit(
                task,
                item_overrides or None,
                fleet_id=fleet_id,
                label=it.get("label"),
                user_id=user_id,
            )
            sessions.append({"session_id": sid, "label": it.get("label")})
        self._append_index(
            {"event": "fleet_submit", "fleet_id": fleet_id, "size": len(sessions)}
        )
        return {"fleet_id": fleet_id, "sessions": sessions}

    def fleet_status(self, fleet_id: str) -> dict:
        """Roll one fleet up: member statuses + summed COGS + status counts.

        Cost is summed from each member's own recorded cost surface (the same
        ``extract_cost`` output stored in the index at terminal), so a fleet's
        total COGS is exact per-session even though static specialist cost does
        not roll into a parent run's total.
        """
        members = [s for s in self.list_sessions() if s.get("fleet_id") == fleet_id]
        if not members:
            raise KeyError(f"unknown fleet_id {fleet_id!r}")
        return {"fleet_id": fleet_id, **_rollup(members)}

    def list_fleets(self) -> list[dict]:
        """Summarize every fleet (newest first): size, status counts, total COGS."""
        by_fleet: dict[str, list[dict]] = {}
        for s in self.list_sessions():
            fid = s.get("fleet_id")
            if fid:
                by_fleet.setdefault(fid, []).append(s)
        out = []
        for fid, members in by_fleet.items():
            roll = _rollup(members)
            started = [m.get("started_ts") for m in members if m.get("started_ts")]
            out.append(
                {
                    "fleet_id": fid,
                    "size": len(members),
                    "counts": roll["counts"],
                    "totals": roll["totals"],
                    "started_ts": min(started) if started else None,
                }
            )
        return sorted(out, key=lambda f: f.get("started_ts") or 0, reverse=True)

    def _start_ask_listener(self, session_id: str, trace_path: Path) -> AskUserListener:
        _ASK_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        sock_path = str(_ASK_SOCKET_DIR / f"{session_id[:12]}.sock")

        def on_question(q: dict) -> None:
            event = {"type": EventType.ASK_USER.value}
            event.update({k: v for k, v in q.items() if v is not None})
            append_trace_event(trace_path, event)
            from fabri.service import slack_events

            task = self._meta.get(session_id, {}).get("task", "")
            routed = slack_events.route_question_to_thread(
                self._slack_cfg,
                session_id,
                task,
                question_id=q.get("question_id", ""),
                question=q.get("question", ""),
                options=q.get("options"),
                default=q.get("default"),
            )
            if not routed:
                post_ask_user_question(
                    self._slack_cfg,
                    session_id=session_id,
                    question=q.get("question", ""),
                    question_id=q.get("question_id", ""),
                    options=q.get("options"),
                    default=q.get("default"),
                )

        listener = AskUserListener(sock_path, on_question)
        listener.start()
        self._asks[session_id] = listener
        return listener

    def answer(
        self,
        session_id: str,
        question_id: str,
        answer: str,
        selected_option: str | None = None,
    ) -> None:
        """Deliver a browser answer to a run's pending ask_user question."""
        listener = self._asks.get(session_id)
        if listener is None:
            raise KeyError(f"no active ask_user bridge for session {session_id!r}")
        if not listener.answer(question_id, answer, selected_option):
            raise KeyError(
                f"no pending question {question_id!r} for session {session_id!r}"
            )

    def list_pending_questions(self) -> list[dict]:
        """Return every pending ask_user question across live runs, oldest first."""
        questions: list[dict] = []
        for session_id, listener in self._asks.items():
            meta = self._meta.get(session_id, {})
            for question in listener.pending_questions():
                entry = {
                    **question,
                    "session_id": session_id,
                    "task": meta.get("task"),
                }
                for key in ("label", "thread_id", "fleet_id"):
                    if meta.get(key) is not None:
                        entry[key] = meta[key]
                questions.append(entry)
        return sorted(questions, key=lambda question: question["asked_ts"])

    def _close_ask(self, session_id: str) -> None:
        listener = self._asks.pop(session_id, None)
        if listener is not None:
            listener.close()

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
        # The run has ended: no more questions can arrive, so release the bridge.
        self._close_ask(session_id)
        # Read the completed trace under the run's own home for the cost surface.
        events = _read_trace_at(handle.trace_path)
        cost = extract_cost(events)
        self.sync_hook.sync_out(session_id, handle.fabri_home, [])
        result = {
            "session_id": session_id,
            "success": envelope.get("success"),
            "outcome": envelope.get("outcome"),
            "final_text": envelope.get("final_text"),
            "structured_output": envelope.get("structured_output"),
            "usage": envelope.get("usage"),
            "cost": cost,
            "error": envelope.get("error"),
        }
        self._finalize(
            session_id,
            event="result",
            outcome=envelope.get("outcome"),
            success=envelope.get("success"),
            cost=cost,
        )
        return result

    def cancel(self, session_id: str) -> dict:
        """Terminate a still-running agent + release its ask_user bridge.

        Idempotent: cancelling an already-finished run is a no-op that still
        returns its recorded status. Records a terminal ``cancel`` index line so
        history reflects the interruption after a restart.
        """
        handle = self._handle(session_id)  # KeyError -> 404 at the HTTP layer
        was_running = handle.is_running()
        handle.terminate()
        self._close_ask(session_id)
        if was_running:
            self._finalize(session_id, event="cancel", outcome="cancelled")
        return {"session_id": session_id, "status": "cancelled" if was_running else "already_ended"}

    def _finalize(
        self,
        session_id: str,
        *,
        event: str,
        outcome: str | None = None,
        success: bool | None = None,
        cost: dict | None = None,
    ) -> None:
        """Write a session's one terminal index line (guarded against dupes)."""
        if session_id in self._finalized:
            return
        self._finalized.add(session_id)
        meta = self._meta.get(session_id, {})
        self._append_index(
            {
                "event": event,
                "session_id": session_id,
                "task": meta.get("task"),
                "thread_id": meta.get("thread_id"),
                "fleet_id": meta.get("fleet_id"),
                "outcome": outcome,
                "success": success,
                "cost": cost,
            }
        )
        self.run_store.record_terminal(
            session_id=session_id,
            finished_at=time.time(),
            event=event,
            outcome=outcome,
            cost=cost,
        )

    def list_sessions(
        self, *, agency: str | None = None, limit: int | None = None, offset: int = 0,
        user_id: str | None = None,
    ) -> list[dict]:
        """Return a summary of every known run, newest first.

        Read from the durable SQLite index with live handles overlaid: a session
        whose subprocess is still alive reads
        ``running`` even if no terminal line exists yet. Each entry:
        ``{session_id, task, status, outcome, thread_id, fleet_id, started_ts,
        cost}``.
        """
        summaries = {
            entry["session_id"]: entry
            for entry in self.run_store.list_runs(
                agency=agency, limit=limit, offset=offset, user_id=user_id
            )
        }
        # Overlay live state: a handle still running trumps a stale "running"
        # index entry that never got a terminal line (e.g. after a crash the
        # entry stays "running" -- but if the handle is gone we can't tell, so we
        # only *upgrade* known-live handles, never mark unknown ones failed).
        for sid, handle in self._runs.items():
            if sid in summaries and handle.is_running():
                summaries[sid]["status"] = "running"
        return list(summaries.values())

    def list_agencies(self) -> list[dict]:
        """Return per-agency dashboard cost and reuse series, oldest first."""
        return self.run_store.agency_aggregates()

    def close(self) -> None:
        """Terminate any still-running children + release ask_user bridges."""
        for handle in self._runs.values():
            handle.terminate()
        for listener in list(self._asks.values()):
            listener.close()
        self._asks.clear()


_SUCCESS_OUTCOMES = frozenset({"success", "success_with_recovery"})


def _rollup(members: list[dict]) -> dict:
    """Aggregate a set of session summaries into a fleet roll-up.

    Returns ``{sessions, counts, totals}`` where ``totals`` sums each member's
    own ``total_cost_usd`` and merges their ``cost_by_model`` maps. Members still
    running contribute a status count but no cost (their surface is None until
    they finish).
    """
    counts: dict[str, int] = {}
    total = 0.0
    by_model: dict[str, float] = {}
    for m in members:
        counts[m.get("status", "running")] = counts.get(m.get("status", "running"), 0) + 1
        cost = m.get("cost") or {}
        total += cost.get("total_cost_usd") or 0.0
        for model, c in (cost.get("cost_by_model") or {}).items():
            by_model[model] = by_model.get(model, 0.0) + (c or 0.0)
    return {
        "sessions": members,
        "counts": counts,
        "totals": {
            "total_cost_usd": round(total, 6),
            "cost_by_model": {k: round(v, 6) for k, v in by_model.items()},
        },
    }


def _status_for(event: str | None, outcome: str | None) -> str:
    """Map a terminal index record to a coarse UI status."""
    if event == "cancel":
        return "cancelled"
    if outcome in _SUCCESS_OUTCOMES:
        return "done"
    return "error"


def _read_index(path: Path) -> list[dict]:
    """Read the append-only session index; skip malformed lines. Empty if absent."""
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
