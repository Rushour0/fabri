"""Invite-only Studio auth: strict route gating, allow lists, signup off, CLI users."""
from __future__ import annotations

import io
import sys
import threading

import httpx
import pytest

from fabri import cli
from fabri.config import ConfigError, load_auth_config
from fabri.service.auth import UserStore, sign_session
from fabri.service.http_server import serve_http
from fabri.service.service import FabriService

BOSS, DEV, OUTSIDER = "boss@x.test", "dev@x.test", "outsider@x.test"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN", "FABRI_AUTH_ALLOWED_EMAILS",
                 "FABRI_AUTH_ADMINS", "FABRI_AUTH_SIGNUP", "FABRI_AUTH_DB_PATH", "FABRI_AUTH_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_AUTH_SECRET", "secret")


def write_auth(tmp_path, body: str):
    path = tmp_path / "fabri-service.yaml"
    path.write_text(body)
    return path


INVITE_ONLY = f"""auth:
  enabled: true
  secure_cookie: false
  signup: true
  admins: [{BOSS}]
  allowed_emails: [{BOSS}, {DEV}]
"""


@pytest.fixture
def make_server(tmp_path):
    started = []

    def make(auth_body: str = INVITE_ONLY):
        service = FabriService(home_root=tmp_path / "home", auth_config=write_auth(tmp_path, auth_body))
        submitted = []
        service.submit = lambda task, overrides, **kw: submitted.append((task, kw)) or "run-1"
        server = serve_http(service, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, service))
        return server, service, submitted

    yield make
    for server, service in started:
        server.shutdown()
        server.server_close()
        service.close()


def client(server, headers=None):
    return httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", headers=headers or {})


def session(service, email):
    users = service.user_store
    user_id = users.verify_user(email, "password-1") or users.create_user(email, "password-1")
    return {"Cookie": f"fabri_session={sign_session(user_id, 'secret', 60)}"}


def test_anonymous_callers_get_only_health_and_auth(make_server):
    server, _, submitted = make_server()
    anon = client(server)
    assert anon.get("/health").status_code == 200
    assert anon.get("/auth/config").json() == {"signup": True, "guests": False}
    assert anon.get("/auth/check").status_code == 401
    for path in ("/runs", "/fleets", "/agencies", "/questions", "/company", "/catalog",
                 "/runs/x/result", "/slack/installs"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/runs", json={"task": "t"}).status_code == 401
    assert anon.put("/nowhere").status_code == 401
    assert anon.delete("/nowhere").status_code == 401
    assert submitted == []


def test_session_reads_but_only_admins_change_work(make_server):
    server, service, submitted = make_server()
    dev = client(server, session(service, DEV))
    boss = client(server, session(service, BOSS))
    assert dev.get("/auth/check").status_code == 204
    assert dev.get("/runs").status_code == 200
    assert dev.post("/runs", json={"task": "t"}).status_code == 403
    assert dev.post("/fleets", json={"items": [{"task": "t"}]}).status_code == 403
    assert dev.post("/runs/x/cancel").status_code == 403
    assert dev.get("/github/setup?installation_id=1").status_code == 403
    assert submitted == []
    assert boss.post("/runs", json={"task": "t"}).json() == {"session_id": "run-1", "status": "submitted"}
    assert submitted[0][1]["user_id"] == service.user_store.verify_user(BOSS, "password-1")


def test_tokens_admin_writes_service_reads(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
    server, _, submitted = make_server()
    svc = client(server, {"Authorization": "Bearer svc"})
    adm = client(server, {"Authorization": "Bearer adm"})
    assert svc.get("/runs").status_code == 200
    assert svc.get("/auth/check").status_code == 204
    assert svc.post("/runs", json={"task": "t"}).status_code == 403
    assert client(server, {"Authorization": "Bearer nope"}).get("/runs").status_code == 401
    assert adm.post("/runs", json={"task": "t"}).status_code == 200
    assert submitted == [("t", {"catalog_ref": None, "user_id": None})]


def test_another_users_run_stays_hidden(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    server, service, _ = make_server()
    service.run_owner = lambda _sid: "someone-else"
    service.result = lambda sid, **_: {"session_id": sid}
    dev = client(server, session(service, DEV))
    assert dev.get("/runs/r1/result").status_code == 404
    assert client(server, {"Authorization": "Bearer adm"}).get("/runs/r1/result").json() == {"session_id": "r1"}


def test_allow_list_refuses_other_emails_with_the_generic_answers(make_server):
    server, service, _ = make_server()
    anon = client(server)
    creds = {"email": OUTSIDER, "password": "password-1"}
    assert anon.post("/auth/signup", json=creds).json() == {"ok": True}
    assert service.user_store.verify_user(OUTSIDER, "password-1") is None
    bad = anon.post("/auth/login", json=creds)
    wrong = anon.post("/auth/login", json={"email": DEV, "password": "nope-nope"})
    assert (bad.status_code, bad.json()) == (wrong.status_code, wrong.json()) == (401, {"error": "invalid email or password"})

    assert anon.post("/auth/signup", json={"email": DEV, "password": "password-1"}).json() == {"ok": True}
    ok = anon.post("/auth/login", json={"email": DEV, "password": "password-1"})
    assert ok.status_code == 200 and "fabri_session=" in ok.headers["set-cookie"]


def test_session_of_a_dropped_email_stops_working(make_server):
    server, service, _ = make_server()
    outsider_id = service.user_store.create_user(OUTSIDER, "password-1")
    cookie = {"Cookie": f"fabri_session={sign_session(outsider_id, 'secret', 60)}"}
    c = client(server, cookie)
    assert c.get("/runs").status_code == 401
    assert c.get("/auth/me").status_code == 401
    assert c.get("/auth/check").status_code == 401


def test_signup_can_be_turned_off(make_server):
    server, service, _ = make_server(INVITE_ONLY.replace("signup: true", "signup: false"))
    anon = client(server)
    assert anon.get("/auth/config").json()["signup"] is False
    r = anon.post("/auth/signup", json={"email": DEV, "password": "password-1"})
    assert (r.status_code, r.json()) == (403, {"error": "signup is disabled"})
    assert service.user_store.verify_user(DEV, "password-1") is None


def test_guest_mode_keeps_the_old_open_routes(make_server):
    server, _, submitted = make_server(INVITE_ONLY + "  allow_guests: true\n")
    anon = client(server)
    assert anon.get("/catalog").status_code == 200
    assert anon.post("/runs", json={"task": "t"}).status_code == 200
    assert submitted == [("t", {"catalog_ref": None, "user_id": None})]


def test_auth_check_without_session_auth(tmp_path, monkeypatch):
    service = FabriService(home_root=tmp_path / "home")
    server = serve_http(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert client(server).get("/auth/check").status_code == 204
        monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
        assert client(server).get("/auth/check").status_code == 401
        assert client(server, {"Authorization": "Bearer svc"}).get("/auth/check").status_code == 204
    finally:
        server.shutdown(); server.server_close(); service.close()


def test_load_auth_config_env_and_errors(tmp_path, monkeypatch):
    path = write_auth(tmp_path, INVITE_ONLY)
    cfg = load_auth_config(path)
    assert cfg["allowed_emails"] == [BOSS, DEV] and cfg["session_secret_env"] == "FABRI_AUTH_SECRET"
    monkeypatch.setenv("FABRI_AUTH_ALLOWED_EMAILS", " a@x.test, b@x.test ,")
    monkeypatch.setenv("FABRI_AUTH_SIGNUP", "false")
    cfg = load_auth_config(path)
    assert cfg["allowed_emails"] == ["a@x.test", "b@x.test"] and cfg["signup"] is False
    with pytest.raises(ConfigError):
        load_auth_config(write_auth(tmp_path, "auth: [nope]\n"))
    with pytest.raises(ConfigError):
        load_auth_config(write_auth(tmp_path, "auth: {allowed_emails: a@x.test}\n"))
    with pytest.raises(ConfigError):
        load_auth_config(tmp_path / "missing.yaml")


def run_cli(monkeypatch, argv, stdin):
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    cli.main()


def test_cli_add_user_and_set_password(tmp_path, monkeypatch, capsys):
    db = tmp_path / "users.db"
    path = write_auth(tmp_path, INVITE_ONLY + f"  db_path: {db}\n")
    run_cli(monkeypatch, ["auth", "add-user", DEV, "--auth-config", str(path)], "password-1\n")
    assert UserStore(db).verify_user(DEV, "password-1")
    assert "password-1" not in capsys.readouterr().out

    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["auth", "add-user", OUTSIDER, "--auth-config", str(path)], "password-1\n")
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["auth", "add-user", DEV, "--auth-config", str(path)], "password-1\n")
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["auth", "set-password", DEV, "--auth-config", str(path)], "short\n")

    run_cli(monkeypatch, ["auth", "set-password", DEV, "--auth-config", str(path)], "password-2\n")
    assert UserStore(db).verify_user(DEV, "password-1") is None
    assert UserStore(db).verify_user(DEV, "password-2")
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["auth", "set-password", BOSS, "--auth-config", str(path)], "password-2\n")


def test_cli_default_store_matches_the_server_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_HOME", str(tmp_path / "fh"))
    path = write_auth(tmp_path, INVITE_ONLY)
    run_cli(monkeypatch, ["auth", "add-user", BOSS, "--auth-config", str(path)], "password-1\n")
    service = FabriService(auth_config=path)
    try:
        assert service.user_store.verify_user(BOSS, "password-1")
    finally:
        service.close()
