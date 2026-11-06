"""With auth off, every mutation on fabri serve refuses cross-site requests
and needs a provably same-origin request or the service or admin token.
Studio's own same-origin fetches pass; signed webhooks are not checked."""
from __future__ import annotations

import threading

import httpx
import pytest

from fabri.service.http_server import serve_http
from fabri.service.service import FabriService
from fabri.service.setups_routes import SetupsApi

SAME = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN", "FABRI_PUBLIC_BASE_URL", "FABRI_AUTH_ENABLED",
                 "FABRI_AUTH_CONFIG", "FABRI_AUTH_ALLOWED_EMAILS", "FABRI_AUTH_ADMINS"):
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
        root = tmp_path / "setups"
        (root / "companies" / "acme").mkdir(parents=True, exist_ok=True)
        service = FabriService(home_root=tmp_path / "home", **kwargs)
        calls: list[tuple] = []
        service.submit = lambda task, overrides, **kw: calls.append(("submit", task)) or "run-1"
        service.submit_fleet = lambda items, overrides, **kw: calls.append(("fleet",)) or {
            "fleet_id": "f1", "sessions": []}
        service.answer = lambda sid, qid, ans, opt=None: calls.append(("answer", sid))
        service.cancel = lambda sid: calls.append(("cancel", sid)) or {"status": "cancelled"}
        server = serve_http(service, port=0, setups_api=SetupsApi(root, policy="strict"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, service))
        port = server.server_address[1]
        return httpx.Client(base_url=f"http://127.0.0.1:{port}"), port, calls

    yield make
    for server, service in started:
        server.shutdown()
        server.server_close()
        service.close()


# (method, path, json body or None). Each changes state when it is let through.
MUTATIONS = [
    ("POST", "/runs", {"task": "t"}),
    ("POST", "/fleets", {"items": [{"task": "t"}]}),
    ("POST", "/runs/run-1/answer", {"question_id": "q", "answer": "a"}),
    ("POST", "/runs/run-1/cancel", None),
    ("POST", "/slack/installs/T1/delete", None),
    ("POST", "/github/installs/1/delete", None),
    ("POST", "/linear/installs/W1/delete", None),
    ("POST", "/setups/scopes", {"kind": "org", "name": "beta"}),
    ("PUT", "/setups/item?scope=org:acme&kind=skill&name=s1", {"files": {"SKILL.md": "# s1\n"}}),
    ("DELETE", "/setups/item?scope=org:acme&kind=skill&name=s1", None),
    ("POST", "/no/such/route", None),
]


def _send(http, method, path, body, headers):
    if body is None:
        return http.request(method, path, headers=headers)
    return http.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize("method, path, body", MUTATIONS)
def test_auth_off_mutations_refuse_cross_site_and_unproven_requests(make_server, method, path, body):
    http, port, calls = make_server()
    own = f"http://127.0.0.1:{port}"
    refused = [
        {},                                                     # a plain request proves nothing
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "cross-site", "Origin": own},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "none"},                             # a typed URL cannot POST
        {"Sec-Fetch-Site": "same-origin", "Host": "rebound.example"},  # DNS rebinding
        {"Origin": "http://rebound.example:%d" % port, "Host": "rebound.example:%d" % port},
    ]
    for headers in refused:
        resp = _send(http, method, path, body, headers)
        assert resp.status_code == 403, (headers, resp.status_code, resp.text)
    assert calls == []


@pytest.mark.parametrize("method, path, body", MUTATIONS)
def test_studio_same_origin_requests_pass(make_server, method, path, body):
    http, port, _calls = make_server()
    for headers in (SAME, {"Origin": f"http://127.0.0.1:{port}"},
                    {"Origin": f"http://localhost:{port}", "Host": f"localhost:{port}", **SAME}):
        resp = _send(http, method, path, body, headers)
        assert resp.status_code not in (403, 415), (headers, resp.status_code, resp.text)


def test_studio_calls_reach_the_service(make_server):
    http, _port, calls = make_server()
    assert http.post("/runs", json={"task": "t"}, headers=SAME).json()["session_id"] == "run-1"
    assert http.post("/fleets", json={"items": [{"task": "t"}]}, headers=SAME).status_code == 200
    assert http.post("/runs/run-1/answer", json={"question_id": "q", "answer": "a"},
                     headers=SAME).status_code == 200
    # Studio sends cancel and the install deletes with no JSON body.
    assert http.post("/runs/run-1/cancel", headers=SAME).status_code == 200
    assert http.post("/slack/installs/T1/delete", headers=SAME).json() == {"deleted": False}
    assert calls == [("submit", "t"), ("fleet",), ("answer", "run-1"), ("cancel", "run-1")]


@pytest.mark.parametrize("path, body", [
    ("/runs", b'{"task": "t"}'),
    ("/fleets", b'{"items": [{"task": "t"}]}'),
    ("/runs/run-1/answer", b'{"question_id": "q", "answer": "a"}'),
    ("/setups/scopes", b'{"kind": "org", "name": "beta"}'),
])
def test_json_routes_need_the_json_content_type(make_server, path, body):
    http, _port, calls = make_server()
    for ctype in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = dict(SAME)
        if ctype:
            headers["Content-Type"] = ctype
        resp = http.post(path, content=body, headers=headers)
        assert resp.status_code == 415, (ctype, resp.text)
    resp = http.put("/setups/item?scope=org:acme&kind=skill&name=s1", content=b'{"files": {}}',
                    headers={**SAME, "Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert calls == []


def test_a_token_stands_in_for_browser_headers(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc-token")
    http, _port, calls = make_server()
    auth = {"Authorization": "Bearer svc-token"}
    assert http.post("/runs", json={"task": "t"}, headers=auth).status_code == 200
    assert http.post("/runs", json={"task": "t"}, headers={"Authorization": "Bearer wrong"}).status_code == 403
    # A token never makes a cross-site request acceptable.
    assert http.post("/runs", json={"task": "t"},
                     headers={**auth, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert http.post("/runs", json={"task": "t"},
                     headers={**auth, "Origin": "https://evil.example"}).status_code == 403
    assert calls == [("submit", "t")]


def test_public_base_origin_and_host_count_as_same_origin(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", "http://127.0.0.1:9999")
    http, _port, calls = make_server()
    assert http.post("/runs", json={"task": "t"},
                     headers={"Origin": "http://127.0.0.1:9999"}).status_code == 200
    assert calls == [("submit", "t")]


def test_webhooks_skip_the_browser_checks(make_server):
    http, _port, _calls = make_server()
    for path in ("/github/webhook", "/slack/events"):
        resp = http.post(path, content=b"{}", headers={"Content-Type": "application/json"})
        assert "cross-site" not in resp.text and "same-origin" not in resp.text, (path, resp.text)


def test_patch_has_no_route(make_server):
    http, _port, _calls = make_server()
    assert http.request("PATCH", "/runs", json={"task": "t"}, headers=SAME).status_code == 501


def test_auth_on_keeps_its_own_rules(make_server, monkeypatch):
    monkeypatch.setenv("FABRI_ADMIN_TOKEN", "adm")
    http, _port, calls = make_server("auth:\n  enabled: true\n  secure_cookie: false\n")
    # An admin token from a script needs no browser headers when auth is on.
    assert http.post("/runs", json={"task": "t"}, headers={"Authorization": "Bearer adm"}).status_code == 200
    assert http.post("/runs", json={"task": "t"}).status_code == 401
    assert calls == [("submit", "t")]
