"""Human-in-the-loop bridge for ``fabri serve``.

The ``ask_user`` tool (``tools/examples/ask_user.py``) routes a question to
``FABRI_ASK_USER_SOCKET`` and blocks on the reply. On the plain CLI a human
answers over stdin; over the HTTP service there is no human at a terminal, so
this listener stands in:

- It owns one ``AF_UNIX`` socket per run and accepts a connection per question
  (arbitrarily many, including from nested ``spawn_subagent`` children, which
  inherit the env var).
- When a question arrives it fires ``on_question`` -- the service uses that to
  append an ``ask_user`` event to the run's trace, so the question reaches the
  browser over the *existing* SSE stream, carrying its ``question_id`` (the
  ``tool_started`` event the agent loop emits does not).
- The connection is held open until the browser answers via
  ``POST /runs/<id>/answer`` -> :meth:`answer`, which writes the reply line the
  tool is waiting for.

Concurrency: multiple questions can be pending at once (a parallel step may fan
out several ``ask_user``-calling sub-agents); each is a distinct connection keyed
by ``question_id``. Answering is idempotent-safe and tolerant of a client that
already timed out and hung up (its 300s socket timeout unwinds independently).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fabri.core.logging_setup import get_logger

logger = get_logger()

# How long a held-open question waits for a browser answer before the handler
# thread gives up and releases the connection. Matches the tool's own client
# timeout ceiling so we don't leak threads indefinitely on an abandoned run.
_ANSWER_WAIT_S = 3600.0
_ACCEPT_POLL_S = 0.5


class _Pending:
    """One question awaiting an answer: the live connection + a latch."""

    def __init__(
        self,
        conn: socket.socket,
        question: str | None,
        options: list[str] | None,
        default: str | None,
    ) -> None:
        self.conn = conn
        self.question = question
        self.options = options
        self.default = default
        self.asked_ts = time.time()
        self.event = threading.Event()
        self.reply: dict | None = None


class AskUserListener:
    """Per-run Unix-socket server that bridges ``ask_user`` to HTTP answers."""

    def __init__(self, socket_path: str, on_question: Callable[[dict], None]) -> None:
        self.socket_path = socket_path
        self._on_question = on_question
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Bind + listen, then accept questions on a background thread."""
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()  # clear any stale socket file
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(16)
        server.settimeout(_ACCEPT_POLL_S)
        self._server = server
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="ask-user-accept", daemon=True
        )
        self._accept_thread.start()

    def close(self) -> None:
        """Stop accepting, release every pending question, unlink the socket."""
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for p in pendings:
            p.event.set()  # unblock the handler; reply stays None -> no write
            try:
                p.conn.close()
            except OSError:
                pass
        try:
            Path(self.socket_path).unlink()
        except FileNotFoundError:
            pass

    # -- answering ------------------------------------------------------------

    def answer(
        self, question_id: str, answer: str, selected_option: str | None = None
    ) -> bool:
        """Deliver a browser answer to a pending question.

        Returns ``True`` if a matching pending question was found and released,
        ``False`` if there's no such question (already answered, timed out, or
        never asked).
        """
        with self._lock:
            pending = self._pending.get(question_id)
        if pending is None:
            return False
        reply: dict = {"question_id": question_id, "answer": answer}
        if selected_option is not None:
            reply["selected_option"] = selected_option
        pending.reply = reply
        pending.event.set()
        return True

    def pending_questions(self) -> list[dict]:
        """Return snapshots of questions that are still awaiting an answer."""
        with self._lock:
            return [
                {
                    "question_id": question_id,
                    "question": pending.question,
                    "options": pending.options,
                    "default": pending.default,
                    "asked_ts": pending.asked_ts,
                }
                for question_id, pending in self._pending.items()
                if not pending.event.is_set()
            ]

    # -- internals ------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,), name="ask-user-conn", daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        question_id: str | None = None
        try:
            line = _read_line(conn)
            if not line:
                return
            req = json.loads(line)
            question_id = req.get("question_id")
            if not question_id:
                return
            pending = _Pending(
                conn,
                req.get("question"),
                req.get("options"),
                req.get("default"),
            )
            with self._lock:
                self._pending[question_id] = pending

            # Surface the question (the service turns this into an SSE-visible
            # trace event). Best-effort: a failure here must not strand the tool.
            try:
                self._on_question(
                    {
                        "question_id": question_id,
                        "question": req.get("question"),
                        "options": req.get("options"),
                        "default": req.get("default"),
                    }
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug("ask_user on_question hook failed", exc_info=True)

            # Block until the browser answers (or the run tears down).
            pending.event.wait(timeout=_ANSWER_WAIT_S)
            if pending.reply is not None:
                try:
                    conn.sendall((json.dumps(pending.reply) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # Client already gave up (its own 300s timeout) and hung up.
                    pass
        except Exception:  # pragma: no cover - defensive
            logger.debug("ask_user connection handler failed", exc_info=True)
        finally:
            if question_id is not None:
                with self._lock:
                    self._pending.pop(question_id, None)
            try:
                conn.close()
            except OSError:
                pass


def _read_line(conn: socket.socket, timeout: float = 10.0) -> str:
    """Read one newline-terminated line off the connection."""
    conn.settimeout(timeout)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
    except (socket.timeout, OSError):
        return ""
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace")


def append_trace_event(trace_path: str | Path, event: dict) -> None:
    """Append one event to a run's trace JSONL by absolute path.

    Mirrors :func:`fabri.orchestrator.traces.log_event` (same ``ts`` stamping and
    ``flock`` cross-process append), but targets an explicit path -- the service
    runs under a different ``FABRI_HOME`` than the run whose trace it's writing.
    """
    import fcntl  # posix-only, same as traces.log_event

    path = Path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), **event}
    line = json.dumps(record) + "\n"
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)
