from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from fabri.service.auth import (
    UserStore,
    build_clear_cookie,
    build_set_cookie,
    parse_cookie,
    sign_session,
    verify_session,
)
import fabri.service.auth as auth
from fabri.service.http_server import _Handler
from fabri.service.run_store import RunStore


def test_user_store_creates_and_verifies_users(tmp_path):
    store = UserStore(tmp_path / "auth.db")
    user_id = store.create_user(" User@example.com ", "correct-password")

    assert store.verify_user("user@example.com", "correct-password") == user_id
    assert store.verify_user("user@example.com", "wrong-password") is None
    assert store.verify_user("nobody@example.com", "correct-password") is None
    assert store.get_user(user_id) == {"id": user_id, "email": "user@example.com"}

    try:
        store.create_user("USER@example.com", "another-password")
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate email must fail")


def test_unknown_email_still_performs_scrypt(tmp_path, monkeypatch):
    store = UserStore(tmp_path / "auth.db")
    calls = []
    original_scrypt = auth.hashlib.scrypt

    def record_scrypt(*args, **kwargs):
        calls.append((args, kwargs))
        return original_scrypt(*args, **kwargs)

    monkeypatch.setattr(auth.hashlib, "scrypt", record_scrypt)

    assert store.verify_user("nobody@example.com", "correct-password") is None
    assert len(calls) == 1
    assert calls[0][1]["salt"] == auth._DUMMY_SALT


def test_run_owner_returns_only_the_persisted_owner(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    store.record_submit(
        session_id="owned-run",
        agency="default",
        task="task",
        submitted_at=1.0,
        user_id="user-1",
    )
    store.record_submit(
        session_id="legacy-run", agency="default", task="task", submitted_at=2.0
    )

    assert store.run_owner("owned-run") == "user-1"
    assert store.run_owner("legacy-run") is None
    assert store.run_owner("unknown-run") is None


def test_auth_routes_hide_another_users_run():
    service = SimpleNamespace(auth_enabled=True, run_owner=lambda _session_id: "user-a")

    for path, action in (
        ("/runs/owned-run/events", "_stream_events"),
        ("/runs/owned-run/result", "_send_result"),
        ("/runs/owned-run/answer", "_answer"),
        ("/runs/owned-run/cancel", "_cancel"),
    ):
        handler = object.__new__(_Handler)
        handler.server = SimpleNamespace(service=service)
        handler.path = path
        handler._current_user = lambda: "user-b"
        responses = []
        handler._send_json = lambda code, payload: responses.append((code, payload))
        setattr(handler, action, lambda _session_id: AssertionError("must not be called"))

        if action in {"_stream_events", "_send_result"}:
            handler.do_GET()
        else:
            handler.do_POST()

        assert responses == [(404, {"error": "unknown session_id 'owned-run'"})]


def test_auth_routes_allow_unowned_guest_run():
    service = SimpleNamespace(auth_enabled=True, run_owner=lambda _session_id: None)

    for path, action in (
        ("/runs/guest-run/events", "_stream_events"),
        ("/runs/guest-run/result", "_send_result"),
        ("/runs/guest-run/answer", "_answer"),
        ("/runs/guest-run/cancel", "_cancel"),
    ):
        handler = object.__new__(_Handler)
        handler.server = SimpleNamespace(service=service)
        handler.path = path
        handler._current_user = lambda: None
        calls = []
        setattr(handler, action, lambda session_id: calls.append(session_id))

        if action in {"_stream_events", "_send_result"}:
            handler.do_GET()
        else:
            handler.do_POST()

        assert calls == ["guest-run"]


def test_catalog_is_public_when_auth_is_enabled():
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(
        service=SimpleNamespace(auth_enabled=True, auth_secret="secret"),
        catalog={},
    )
    handler.path = "/catalog"
    handler.headers = {}
    responses = []
    handler._send_json = lambda code, payload: responses.append((code, payload))

    handler.do_GET()

    assert responses == [(200, {"catalog": {"agencies": [], "companies": []}})]


def test_submit_allows_guest_and_leaves_run_unowned():
    submitted = []
    service = SimpleNamespace(
        auth_enabled=True,
        submit=lambda task, overrides, **kwargs: submitted.append(
            (task, overrides, kwargs)
        )
        or "guest-run",
    )
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(service=service)
    handler.path = "/runs"
    handler.headers = {"Content-Length": "17"}
    handler.rfile = BytesIO(b'{"task":"try it"}')
    handler._current_user = lambda: None
    responses = []
    handler._send_json = lambda code, payload: responses.append((code, payload))

    handler.do_POST()

    assert submitted == [("try it", None, {"catalog_ref": None, "user_id": None})]
    assert responses == [(200, {"session_id": "guest-run", "status": "submitted"})]


def test_signup_returns_the_same_generic_response_for_existing_email(tmp_path):
    store = UserStore(tmp_path / "auth.db")
    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(
        service=SimpleNamespace(
            user_store=store,
            auth_secret="secret",
            auth_cfg={"secure_cookie": False},
        )
    )
    responses = []
    handler._send_json = lambda code, payload, **kwargs: responses.append((code, payload, kwargs))

    for _ in range(2):
        body = b'{"email":"user@example.com","password":"correct-password"}'
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler._auth_post("/auth/signup")

    assert responses == [(200, {"ok": True}, {}), (200, {"ok": True}, {})]


def test_sessions_reject_tampering_expiry_and_malformed_tokens():
    token = sign_session("user-id", "secret", 60)

    assert verify_session(token, "secret") == "user-id"
    assert verify_session(token + "x", "secret") is None
    assert verify_session(sign_session("user-id", "secret", -1), "secret") is None
    assert verify_session("not-a-token", "secret") is None


def test_cookie_helpers_round_trip_and_secure_toggle():
    token = "user.123.signature"
    secure_cookie = build_set_cookie(token, 60, secure=True)
    insecure_cookie = build_set_cookie(token, 60, secure=False)

    assert parse_cookie(secure_cookie)["fabri_session"] == token
    assert "; Secure" in secure_cookie
    assert "; Secure" not in insecure_cookie
    assert "Max-Age=0" in build_clear_cookie(secure=True)
