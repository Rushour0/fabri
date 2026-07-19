"""B7 -- minimal HTTP transport for the embeddable service (stdlib only).

A non-Python host starts ``fabri serve`` and drives runs over plain HTTP -- no
fabri imports, no websockets dependency. Three endpoints:

- ``POST /runs``            body ``{"task": ..., "overrides": {...}?}`` ->
                            ``{"session_id": ...}``. Launches the agent.
- ``GET  /runs``            ``{"sessions": [...]}`` -- history of every known
                            run (survives restart; see ``list_sessions``).
- ``GET  /agencies``        per-agency run, cost, and reuse aggregates.
- ``GET  /questions``       ``{"questions": [...]}`` -- pending ask_user inbox.
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
- ``GET  /company``         ``{"company": {...} | null}`` -- served company org chart.
- ``GET  /catalog``         ``{"catalog": {...} | null}`` -- installed roster entries.

Built on :class:`http.server.ThreadingHTTPServer` so a streaming ``events``
request doesn't block a concurrent ``POST /runs``.
"""
from __future__ import annotations

import json
import mimetypes
import re
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from fabri.core.logging_setup import get_logger
from fabri.catalog import catalog_listing
from fabri.service.slack_events import handle_slack_event
from fabri.service.service import FabriService
from fabri.service.auth import (
    build_clear_cookie,
    build_set_cookie,
    parse_cookie,
    sign_session,
    verify_session,
)

logger = get_logger()

STUDIO_ASSETS_DIR = Path(__file__).parent / "studio_assets"

_EVENTS_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/events/?$")
_RESULT_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/result/?$")
_ANSWER_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/answer/?$")
_CANCEL_RE = re.compile(r"^/runs/([A-Za-z0-9_.-]+)/cancel/?$")
_FLEET_RE = re.compile(r"^/fleets/([A-Za-z0-9_.-]+)/?$")


def studio_assets_available() -> bool:
    """Return whether this installation contains a built Studio bundle."""
    return (STUDIO_ASSETS_DIR / "index.html").is_file()


class _Handler(BaseHTTPRequestHandler):
    server_version = "fabri-serve/1"

    @property
    def service(self) -> FabriService:
        return cast("FabriHTTPServer", self.server).service

    @property
    def serve_studio(self) -> bool:
        return cast("FabriHTTPServer", self.server).serve_studio

    @property
    def slack_cfg(self) -> dict:
        return self.service._slack_cfg

    def log_message(self, fmt: str, *args) -> None:  # quiet the default stderr spam
        logger.debug("fabri serve: " + fmt, *args)

    def _send_json(
        self, code: int, payload: dict, *, extra_headers: Mapping[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _current_user(self) -> str | None:
        if not self.service.auth_enabled:
            return None
        secret = self.service.auth_secret
        if not secret:
            return None
        token = parse_cookie(self.headers.get("Cookie", "")).get("fabri_session", "")
        return verify_session(token, secret)

    def _require_user(self) -> str | None:
        user_id = self._current_user()
        if user_id is None:
            self._send_json(401, {"error": "auth required"})
        return user_id

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.service.auth_enabled and path in ("/auth/me", "/auth/me/"):
            user_id = self._current_user()
            user = (
                self.service.user_store.get_user(user_id)
                if user_id and self.service.user_store
                else None
            )
            if user is None:
                self._send_json(401, {"error": "auth required"})
            else:
                self._send_json(200, {"email": user["email"]})
            return
        if path in ("/health", "/health/"):
            self._send_json(200, {"status": "ok"})
            return
        if path in ("/runs", "/runs/"):
            user_id = self._require_user() if self.service.auth_enabled else None
            if self.service.auth_enabled and user_id is None:
                return
            try:
                filters = self._run_filters(parsed.query)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(
                200, {"sessions": self.service.list_sessions(**filters, user_id=user_id)}
            )
            return
        if path in ("/agencies", "/agencies/"):
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._send_json(200, {"agencies": self.service.list_agencies()})
            return
        if path in ("/questions", "/questions/"):
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._send_json(200, {"questions": self.service.list_pending_questions()})
            return
        if path in ("/company", "/company/"):
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._send_json(200, {"company": self.server.company})
            return
        if path in ("/catalog", "/catalog/"):
            catalog = self.server.catalog
            self._send_json(200, {"catalog": catalog_listing(catalog) if catalog is not None else None})
            return
        if path in ("/fleets", "/fleets/"):
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._send_json(200, {"fleets": self.service.list_fleets()})
            return
        m = _FLEET_RE.match(path)
        if m:
            if self.service.auth_enabled and self._require_user() is None:
                return
            try:
                self._send_json(200, self.service.fleet_status(m.group(1)))
            except KeyError:
                self._send_json(404, {"error": f"unknown fleet_id {m.group(1)!r}"})
            return
        m = _EVENTS_RE.match(path)
        if m:
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._stream_events(m.group(1))
            return
        m = _RESULT_RE.match(path)
        if m:
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._send_result(m.group(1))
            return
        if self.serve_studio and not self._is_api_path(self.path):
            self._send_studio_asset()
            return
        self._send_json(404, {"error": f"no route for GET {self.path}"})

    @staticmethod
    def _run_filters(query: str) -> dict[str, str | int | None]:
        values = parse_qs(query)
        agency = values.get("agency", [None])[0]
        try:
            limit = int(values["limit"][0]) if "limit" in values else None
            offset = int(values.get("offset", ["0"])[0])
        except ValueError as exc:
            raise ValueError("limit and offset must be integers") from exc
        if limit is not None and limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        return {"agency": agency, "limit": limit, "offset": offset}

    def do_POST(self) -> None:  # noqa: N802
        if self.service.auth_enabled and self.path.rstrip("/") in {
            "/auth/signup",
            "/auth/login",
            "/auth/logout",
        }:
            self._auth_post(self.path.rstrip("/"))
            return
        if self.path in ("/slack/events", "/slack/events/"):
            if not self.slack_cfg.get("events_enabled"):
                self._send_json(404, {"error": f"no route for POST {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, body, extra_headers = handle_slack_event(
                raw, self.headers, self.service, self.slack_cfg
            )
            encoded = body.encode("utf-8")
            self.send_response(status)
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        m = _ANSWER_RE.match(self.path)
        if m:
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._answer(m.group(1))
            return
        m = _CANCEL_RE.match(self.path)
        if m:
            if self.service.auth_enabled and self._require_user() is None:
                return
            self._cancel(m.group(1))
            return
        if self.path in ("/fleets", "/fleets/"):
            user_id = self._require_user() if self.service.auth_enabled else None
            if self.service.auth_enabled and user_id is None:
                return
            self._submit_fleet(user_id=user_id)
            return
        if self.path not in ("/runs", "/runs/"):
            self._send_json(404, {"error": f"no route for POST {self.path}"})
            return
        user_id = self._require_user() if self.service.auth_enabled else None
        if self.service.auth_enabled and user_id is None:
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
            session_id = self.service.submit(
                task,
                req.get("overrides"),
                catalog_ref=req.get("catalog_ref"),
                user_id=user_id,
            )
        except KeyError as e:
            self._send_json(400, {"error": str(e)})
            return
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

    def _submit_fleet(self, *, user_id: str | None = None) -> None:
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
            result = self.service.submit_fleet(
                items, req.get("overrides"), user_id=user_id
            )
        except Exception as e:  # bind/launch/validation -> 400, not 500 HTML
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, result)

    def _auth_post(self, path: str) -> None:
        if path == "/auth/logout":
            self._send_json(
                200,
                {"ok": True},
                extra_headers={
                    "Set-Cookie": build_clear_cookie(
                        self.service.auth_cfg.get("secure_cookie", True)
                    )
                },
            )
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid request JSON"})
            return
        email, password = req.get("email"), req.get("password")
        if (
            not isinstance(email, str)
            or not email.strip()
            or not isinstance(password, str)
            or not password
        ):
            self._send_json(400, {"error": "email and password are required"})
            return
        store = self.service.user_store
        secret = self.service.auth_secret
        if store is None or secret is None:
            self._send_json(500, {"error": "authentication is unavailable"})
            return
        if path == "/auth/signup":
            try:
                user_id = store.create_user(email, password)
            except ValueError:
                self._send_json(409, {"error": "email already registered"})
                return
        else:
            user_id = store.verify_user(email, password)
            if user_id is None:
                self._send_json(401, {"error": "invalid email or password"})
                return
        user = store.get_user(user_id)
        if user is None:
            self._send_json(500, {"error": "authentication is unavailable"})
            return
        ttl_s = self.service.auth_cfg.get("session_ttl_s", 604800)
        token = sign_session(user_id, secret, ttl_s)
        self._send_json(
            200,
            {"email": user["email"]},
            extra_headers={
                "Set-Cookie": build_set_cookie(
                    token, ttl_s, self.service.auth_cfg.get("secure_cookie", True)
                )
            },
        )

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

    @staticmethod
    def _is_api_path(raw_path: str) -> bool:
        path = urlsplit(raw_path).path.rstrip("/")
        return any(path == prefix or path.startswith(f"{prefix}/")
                   for prefix in ("/runs", "/fleets", "/agencies", "/health", "/questions", "/company", "/catalog", "/slack", "/auth"))

    def _send_studio_asset(self) -> None:
        request_path = unquote(urlsplit(self.path).path).lstrip("/")
        asset_path = (STUDIO_ASSETS_DIR / request_path).resolve()
        assets_root = STUDIO_ASSETS_DIR.resolve()
        if (asset_path == assets_root or assets_root not in asset_path.parents
                or not asset_path.is_file()):
            asset_path = STUDIO_ASSETS_DIR / "index.html"

        try:
            body = asset_path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "Studio assets are not available"})
            return

        content_type, _ = mimetypes.guess_type(asset_path.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FabriHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server carrying a :class:`FabriService`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: FabriService,
        *,
        serve_studio: bool = False,
        company: dict | None = None,
        catalog: Mapping[str, dict] | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.service = service
        self.serve_studio = serve_studio
        self.company = company
        self.catalog = catalog


def serve_http(
    service: FabriService,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    serve_studio: bool = False,
    company: dict | None = None,
    catalog: Mapping[str, dict] | None = None,
) -> FabriHTTPServer:
    """Build (but do not block on) a :class:`FabriHTTPServer`.

    Returns the server so a caller can ``serve_forever()`` (the CLI does) or run
    it in a thread (tests do). Bind a port of ``0`` to get an OS-assigned one.
    """
    return FabriHTTPServer(
        (host, port), service, serve_studio=serve_studio, company=company, catalog=catalog
    )
