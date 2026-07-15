"""B7 -- minimal HTTP transport for the embeddable service (stdlib only).

A non-Python host starts ``fabri serve`` and drives runs over plain HTTP -- no
fabri imports, no websockets dependency. Three endpoints:

- ``POST /runs``            body ``{"task": ..., "overrides": {...}?}`` ->
                            ``{"session_id": ...}``. Launches the agent.
- ``GET  /runs``            ``{"sessions": [...]}`` -- history of every known
                            run (survives restart; see ``list_sessions``).
- ``GET  /runs/<id>/events`` Server-Sent Events: one ``data:`` frame per trace
                            event (the live :mod:`fabri.events` vocabulary),
                            then a terminal ``event: result`` frame carrying the
                            result envelope + cost surface.
- ``GET  /runs/<id>/result`` blocks for the run and returns the result JSON
                            (convenience for hosts that don't want SSE).
- ``POST /runs/<id>/answer`` body ``{"question_id", "answer", ...}`` -- reply to
                            a mid-run ``ask_user`` question.
- ``POST /runs/<id>/cancel`` terminate a still-running agent -> ``{"status"}``.
- ``POST /fleets``          body ``{"items": [{"task", "label"?, "overrides"?}],
                            "overrides"?}`` -> ``{"fleet_id", "sessions"}``. Fans
                            one batch out to N runs sharing a fleet_id.
- ``GET  /fleets``          ``{"fleets": [...]}`` -- fleet roll-ups.
- ``GET  /fleets/<id>``     one fleet's member statuses + summed COGS.
- ``GET  /health``          ``{"status": "ok"}``.

Built on :class:`http.server.ThreadingHTTPServer` so a streaming ``events``
request doesn't block a concurrent ``POST /runs``.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fabri.core.logging_setup import get_logger
from fabri.service.service import FabriService

logger = get_logger()

_EVENTS_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/events/?$")
_RESULT_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/result/?$")
_ANSWER_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/answer/?$")
_CANCEL_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/cancel/?$")
_FLEET_RE = re.compile(r"^/fleets/([A-Za-z0-9_.-]+)/?$")


class _Handler(BaseHTTPRequestHandler):
    server_version = "fabri-serve/1"

    @property
    def service(self) -> FabriService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # quiet the default stderr spam
        logger.debug("fabri serve: " + fmt, *args)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path in ("/health", "/health/"):
            self._send_json(200, {"status": "ok"})
            return
        if self.path in ("/runs", "/runs/"):
            self._send_json(200, {"sessions": self.service.list_sessions()})
            return
        if self.path in ("/fleets", "/fleets/"):
            self._send_json(200, {"fleets": self.service.list_fleets()})
            return
        m = _FLEET_RE.match(self.path)
        if m:
            try:
                self._send_json(200, self.service.fleet_status(m.group(1)))
            except KeyError:
                self._send_json(404, {"error": f"unknown fleet_id {m.group(1)!r}"})
            return
        m = _EVENTS_RE.match(self.path)
        if m:
            self._stream_events(m.group(1))
            return
        m = _RESULT_RE.match(self.path)
        if m:
            self._send_result(m.group(1))
            return
        self._send_json(404, {"error": f"no route for GET {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        m = _ANSWER_RE.match(self.path)
        if m:
            self._answer(m.group(1))
            return
        m = _CANCEL_RE.match(self.path)
        if m:
            self._cancel(m.group(1))
            return
        if self.path in ("/fleets", "/fleets/"):
            self._submit_fleet()
            return
        if self.path not in ("/runs", "/runs/"):
            self._send_json(404, {"error": f"no route for POST {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid request JSON: {e}"})
            return
        task = req.get("task")
        if not task:
            self._send_json(400, {"error": "request missing required field 'task'"})
            return
        try:
            session_id = self.service.submit(task, req.get("overrides"))
        except Exception as e:  # surface bind/launch errors as 400, not 500 HTML
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, {"session_id": session_id, "status": "submitted"})

    def _answer(self, session_id: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid request JSON: {e}"})
            return
        question_id = req.get("question_id")
        if not question_id or "answer" not in req:
            self._send_json(
                400, {"error": "answer requires 'question_id' and 'answer'"}
            )
            return
        try:
            self.service.answer(
                session_id, question_id, req["answer"], req.get("selected_option")
            )
        except KeyError as e:
            self._send_json(404, {"error": str(e)})
            return
        self._send_json(200, {"status": "answered"})

    def _cancel(self, session_id: str) -> None:
        try:
            result = self.service.cancel(session_id)
        except KeyError:
            self._send_json(404, {"error": f"unknown session_id {session_id!r}"})
            return
        self._send_json(200, result)

    def _submit_fleet(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid request JSON: {e}"})
            return
        items = req.get("items")
        if not isinstance(items, list) or not items:
            self._send_json(400, {"error": "fleet request requires a non-empty 'items' list"})
            return
        try:
            result = self.service.submit_fleet(items, req.get("overrides"))
        except Exception as e:  # bind/launch/validation -> 400, not 500 HTML
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, result)

    def _stream_events(self, session_id: str) -> None:
        try:
            stream = self.service.stream(session_id)
        except KeyError:
            self._send_json(404, {"error": f"unknown session_id {session_id!r}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in stream:
                self._sse(event=None, data=event)
            self._sse(event="result", data=self.service.result(session_id))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _sse(self, *, event: str | None, data: dict) -> None:
        chunk = ""
        if event:
            chunk += f"event: {event}\n"
        chunk += f"data: {json.dumps(data)}\n\n"
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def _send_result(self, session_id: str) -> None:
        try:
            result = self.service.result(session_id)
        except KeyError:
            self._send_json(404, {"error": f"unknown session_id {session_id!r}"})
            return
        self._send_json(200, result)


class FabriHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server carrying a :class:`FabriService`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: FabriService) -> None:
        super().__init__(address, _Handler)
        self.service = service


def serve_http(
    service: FabriService, *, host: str = "127.0.0.1", port: int = 8080
) -> FabriHTTPServer:
    """Build (but do not block on) a :class:`FabriHTTPServer`.

    Returns the server so a caller can ``serve_forever()`` (the CLI does) or run
    it in a thread (tests do). Bind a port of ``0`` to get an OS-assigned one.
    """
    return FabriHTTPServer((host, port), service)
