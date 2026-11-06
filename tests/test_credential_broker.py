"""fabri.service.credential_broker: the service-side broker that sends a
run's credentialed requests so the run never holds a token."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from fabri.service import credential_broker as cb
from fabri.service.ask_user_listener import AskUserListener
from fabri.service.connection_store import ConnectionStore
from fabri.service.credential_broker import (
    BindError,
    CredentialBroker,
    GrantTable,
    bind_sources,
    socket_path_for,
)

TOKEN = "tok-ck-ORIGINAL-1234567890"
NEW_TOKEN = "tok-ck-ROTATED-0987654321"
GH_TOKEN = "ghp_brokertesttoken1234567890"
CK_AUTH = {"Authorization": {"secret_ref": "ck:acme", "prefix": "Bearer "}}
POLICY = {"allowed_kinds": ["http", "openapi", "mcp"], "allow_delete": True}


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every host is public except private.test; no real DNS lookups."""
    def getaddrinfo(host, port, *args, **kwargs):
        address = "10.0.0.5" if host == "private.test" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    for name in list(os.environ):
        if name.startswith("FABRI_CRED_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home():
    path = Path(tempfile.mkdtemp(prefix="fabri-brk-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class FakeListener:
    """Records approval questions and answers them from a queue. An entry
    is an answer string, None (never answered), or a callable taking the
    question text and returning one of those."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.asked: list[dict] = []
        self.forgotten: list[str] = []

    def ask_local(self, *, question_id, question, options, default, kind="ask_user"):
        self.asked.append({"question_id": question_id, "question": question, "options": options,
                           "default": default, "kind": kind})
        pending = SimpleNamespace(event=threading.Event(), reply=None)
        answer = self.answers.pop(0) if self.answers else None
        if callable(answer):
            answer = answer(question)
        if answer is not None:
            pending.reply = {"question_id": question_id, "answer": answer, "selected_option": answer}
            pending.event.set()
        return pending

    def forget(self, question_id):
        self.forgotten.append(question_id)


def _http_decl(name, url, method="GET", headers=None, **extra):
    return {"kind": "http", "name": name, "url": url, "method": method,
            "headers": CK_AUTH if headers is None else headers, **extra}


def _default_sources():
    return [
        _http_decl("ck_get", "https://api.ck.test/lists/{list_id}/contacts"),
        _http_decl("ck_create", "https://api.ck.test/lists/{list_id}/contacts", "POST"),
        _http_decl("ck_send", "https://api.ck.test/campaigns/{campaign_id}/send", "POST"),
        _http_decl("plain", "https://api.plain.test/x", headers={"X-Key": {"env": "PLAIN_KEY"}}),
    ]


def _write_config(home: Path, sources=None, policy=None, agents=None, name="run.yaml") -> Path:
    tools = {"policy": POLICY if policy is None else policy,
             "sources": _default_sources() if sources is None else sources}
    if agents is not None:
        tools["agents"] = agents
    path = home / name
    path.write_text(yaml.safe_dump({"tools": tools}))
    return path


def _store(home: Path, token=TOKEN, **kwargs) -> ConnectionStore:
    store = ConnectionStore(home / "installs.db")
    store.upsert(provider="ck", handle=kwargs.pop("handle", "acme"), access_token=token, **kwargs)
    return store


class Upstream:
    """A MockTransport that records requests and echoes the auth header."""

    def __init__(self, respond=None):
        self.requests: list[httpx.Request] = []
        self._respond = respond
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._respond is not None:
            return self._respond(request)
        return httpx.Response(200, json={"echo": request.headers.get("authorization"),
                                         "path": request.url.path})


def _broker(home, upstream=None, listener=None, sources=None, policy=None, actor=None, sid="sess-0001",
            **kwargs):
    upstream = upstream or Upstream()
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport, **kwargs)
    config = _write_config(home, sources, policy)
    binding = broker.bind(session_id=sid, config_path=config, run_home=home / "runs" / sid[:8], actor=actor,
                          listener=listener, run_pid=os.getpid())
    return broker, binding, upstream


def _line(binding, **fields) -> bytes:
    return json.dumps({"v": 1, "key": binding.key, **fields}).encode()


def _http(broker, binding, tool, source=None, **fields):
    return broker._handle_line(binding, _line(binding, op="http", source=source or tool, tool=tool, **fields))


def _call_socket(path: str, payload: bytes, timeout=10.0) -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    try:
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        s.close()


# ---------------------------------------------------------------- binding


def test_bind_sources_keeps_only_secret_ref_sources(home):
    config = yaml.safe_load(_write_config(home).read_text())
    bound = bind_sources(config, actor=None)
    assert set(bound) == {"ck_get", "ck_create", "ck_send"}
    src = bound["ck_get"]
    assert src.kind == "http"
    assert src.refs == (("ck", "acme"),)
    assert set(src.tools) == {"ck_get"}
    tb = src.tools["ck_get"]
    assert (tb.method, tb.template_url, tb.approval, tb.placeholders) == (
        "GET", "https://api.ck.test/lists/{list_id}/contacts", "read", ("list_id",))
    assert bound["ck_create"].tools["ck_create"].approval == "write"
    assert bound["ck_send"].tools["ck_send"].approval == "always_approve"


def test_bind_sources_substitutes_actor_and_keeps_policy_floor(home):
    sources = [_http_decl("ck_me", "https://api.ck.test/me",
                          headers={"Authorization": {"secret_ref": "ck:${actor}", "prefix": "Bearer "}})]
    policy = {**POLICY, "approval_floor": {"ck_*": "always_approve"}}
    config = yaml.safe_load(_write_config(home, sources, policy).read_text())
    bound = bind_sources(config, actor="u42")
    assert bound["ck_me"].refs == (("ck", "u42"),)
    assert bound["ck_me"].header_specs["Authorization"]["secret_ref"] == "ck:u42"
    assert bound["ck_me"].tools["ck_me"].approval == "always_approve"
    assert bind_sources(config, actor=None)["ck_me"].refs == ()


def test_bind_sources_walks_static_agents_and_refuses_clashes(home):
    sub = _write_config(home, [_http_decl("sub_src", "https://api.sub.test/items")], name="sub.yaml")
    parent = _write_config(home, agents=[{"name": "helper", "config": str(sub)}])
    bound = bind_sources(yaml.safe_load(parent.read_text()), actor=None)
    assert "sub_src" in bound and "ck_get" in bound

    # The same declaration in both configs is fine.
    same = _write_config(home, [_http_decl("ck_get", "https://api.ck.test/lists/{list_id}/contacts")],
                         name="same.yaml")
    parent = _write_config(home, agents=[{"name": "helper", "config": str(same)}])
    assert "ck_get" in bind_sources(yaml.safe_load(parent.read_text()), actor=None)

    clash = _write_config(home, [_http_decl("ck_get", "https://api.ck.test/other")], name="clash.yaml")
    parent = _write_config(home, agents=[{"name": "helper", "config": str(clash)}])
    with pytest.raises(BindError, match="ck_get"):
        bind_sources(yaml.safe_load(parent.read_text()), actor=None)


def test_bind_sources_refuses_ungranted_ref_templated_host_and_stdio_with_ref(home):
    ungranted = {**POLICY, "credential_grants": {"ck_*": ["ck:other"]}}
    config = yaml.safe_load(_write_config(home, [_default_sources()[0]], ungranted).read_text())
    with pytest.raises(BindError, match="ck_get"):
        bind_sources(config, actor=None)

    templated = [_http_decl("ck_region", "https://{region}.ck.test/x")]
    with pytest.raises(BindError, match="ck_region"):
        bind_sources(yaml.safe_load(_write_config(home, templated).read_text()), actor=None)

    stdio = [{"kind": "mcp", "name": "gh", "command": ["gh-mcp"], "headers": CK_AUTH}]
    with pytest.raises(BindError, match="stdio"):
        bind_sources(yaml.safe_load(_write_config(home, stdio).read_text()), actor=None)


def test_bind_creates_0600_socket_in_0700_dir_and_env_names(home):
    broker, binding, _ = _broker(home)
    try:
        sock = Path(binding.socket_path)
        assert stat.S_ISSOCK(sock.stat().st_mode)
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
        env = binding.env()
        assert set(env) == {cb.SOCKET_ENV, cb.KEY_ENV} == {"FABRI_CRED_BROKER_SOCKET", "FABRI_CRED_BROKER_KEY"}
        assert env[cb.SOCKET_ENV] == str(sock)
        assert len(env[cb.KEY_ENV]) == 64
        assert binding.key not in repr(binding)
        assert set(broker.bindings()) == {"sess-0001"}
        with pytest.raises(ValueError):
            broker.bind(session_id="sess-0001", config_path=home / "run.yaml", run_home=home / "r2",
                        actor=None, listener=None)
        # A config with no credentialed source gets no binding at all.
        plain = _write_config(home, [_default_sources()[3]], name="plain.yaml")
        assert broker.bind(session_id="sess-plain", config_path=plain, run_home=home / "r3", actor=None,
                           listener=None) is None
        assert not (home / "r3" / "brk.sock").exists()
    finally:
        broker.close()


def test_long_run_home_falls_back_to_short_dir(home):
    deep = home / ("d" * 60) / ("e" * 60)
    path = socket_path_for(deep, "abcdef0123456789")
    assert len(str(path).encode()) < 104
    assert path.name == "abcdef012345.sock"
    assert path.parent.name == f"fabri-brk-{os.getuid()}"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    short = socket_path_for(home / "r", "abcdef0123456789")
    assert short == home / "r" / "brk.sock"
    assert stat.S_IMODE(short.parent.stat().st_mode) == 0o700


# ---------------------------------------------------------------- request checks


def test_bad_key_is_refused_and_slow(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        for key in ("0" * 64, 12345, None):
            start = time.monotonic()
            reply = broker._handle_line(binding, json.dumps(
                {"v": 1, "key": key, "op": "http", "source": "ck_get", "tool": "ck_get",
                 "path_params": {"list_id": "1"}}).encode())
            assert time.monotonic() - start >= cb.BAD_KEY_DELAY_S
            assert reply["ok"] is False
            assert reply["result"] == {"status": "refused", "code": None}
            assert reply["error"] == "credential broker refused the capability"
        assert upstream.requests == []
    finally:
        broker.close()


def test_unbound_source_tool_method_and_url_shape_are_refused(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        cases = [
            (dict(op="http", source="nope", tool="nope"), "source nope is not bound to this run"),
            (dict(op="http", source="plain", tool="plain"), "source plain is not bound to this run"),
            (dict(op="http", source="ck_get", tool="ck_create", path_params={"list_id": "1"}),
             "not a tool of source ck_get"),
            (dict(op="http", source="ck_get", tool="ck_get", method="DELETE", path_params={"list_id": "1"}),
             "method does not match"),
            (dict(op="http", source="ck_get", tool="ck_get"), "missing path parameter"),
            (dict(op="http", source="ck_get", tool="ck_get", path_params={"list_id": "1", "x": "2"}),
             "unknown path parameters"),
            (dict(op="http", source="ck_get", tool="ck_get", path_params={"list_id": "a/b"}), "not allowed"),
            (dict(op="http", source="ck_get", tool="ck_get", path_params={"list_id": "%2e%2e"}), "not allowed"),
            (dict(op="http", source="ck_get", tool="ck_get", path_params={"list_id": {"a": 1}}),
             "must be a scalar"),
            (dict(op="http", source="ck_get", tool="ck_get", path_params={"list_id": "1"}, params="x=1"),
             "params must be an object"),
            (dict(op="http", source="ck_create", tool="ck_create", path_params={"list_id": "1"},
                  json={"a": 1}, form={"b": 2}), "not both"),
            (dict(op="ftp", source="ck_get", tool="ck_get"), "op must be http or mcp"),
        ]
        for fields, text in cases:
            reply = broker._handle_line(binding, _line(binding, **fields))
            assert reply["ok"] is False, fields
            assert reply["result"]["status"] == "refused", fields
            assert text in reply["error"], (fields, reply["error"])
        bad_version = broker._handle_line(binding, json.dumps(
            {"v": 2, "key": binding.key, "op": "http"}).encode())
        assert "protocol version" in bad_version["error"]
        for raw in (b"not json", b"[1, 2]", b"\xff\xfe"):
            assert broker._handle_line(binding, raw)["result"]["status"] == "refused"
        assert upstream.requests == []
    finally:
        broker.close()


def test_url_is_rendered_from_the_template_and_client_url_is_ignored(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "my list.v2"},
                      url="https://evil.test/steal", params={"page": 2})
        assert reply["ok"] is True
        (sent,) = upstream.requests
        assert sent.url.host == "api.ck.test"
        assert sent.url.raw_path == b"/lists/my%20list.v2/contacts?page=2"
        # The URL-shape check itself, for the parts a rendered value cannot reach.
        check = CredentialBroker._check_url_shape
        template = "https://api.ck.test/lists/{list_id}"
        for bad, text in [
            ("https://api.ck.test/lists/1?x=1", "query or fragment"),
            ("https://api.ck.test/lists/1#f", "query or fragment"),
            ("https://u:p@api.ck.test/lists/1", "userinfo"),
            ("https://api.ck.test/lists/..", "dot segment"),
            ("https://other.test/lists/1", "host differs"),
        ]:
            with pytest.raises(cb._Refuse, match=text):
                check("t", template, bad)
        check("t", template, "https://api.ck.test/lists/1")
    finally:
        broker.close()


def test_read_request_sends_with_injected_header_and_returns_send_shape(home):
    _store(home)
    listener = FakeListener()
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "42"}, timeout_s=9999)
        assert reply["v"] == 1
        assert reply["ok"] is True
        assert reply["result"]["status"] == "ok" and reply["result"]["code"] == 200
        assert reply["result"]["data"]["path"] == "/lists/42/contacts"
        assert reply["result"]["data"]["echo"] == "[redacted]"
        assert TOKEN not in json.dumps(reply)
        (sent,) = upstream.requests
        assert sent.headers["authorization"] == f"Bearer {TOKEN}"
        assert sent.extensions["timeout"]["read"] == cb.MAX_SEND_TIMEOUT_S
        assert listener.asked == []
    finally:
        broker.close()


def test_write_request_sends_without_a_question(home):
    # Orchestrator decision: only always_approve needs a human grant.
    _store(home)
    broker, binding, upstream = _broker(home, listener=None)
    try:
        reply = _http(broker, binding, "ck_create", path_params={"list_id": "1"}, json={"email": "a@b.c"})
        assert reply["ok"] is True
        (sent,) = upstream.requests
        assert sent.method == "POST" and json.loads(sent.content) == {"email": "a@b.c"}
    finally:
        broker.close()


def test_always_approve_request_asks_once_and_sends_on_approve(home):
    _store(home)
    listener = FakeListener(["approve"])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"},
                      json={"subject": "Hello", "password": "hunter2-secret"},
                      extra_headers={"X-Dry-Run": "true"})
        assert reply["ok"] is True
        assert len(listener.asked) == 1
        q = listener.asked[0]
        assert q["kind"] == "approval" and q["options"] == ["approve", "deny"] and q["default"] == "deny"
        text = q["question"]
        assert "ck_send needs your approval (class always_approve)" in text
        assert "POST https://api.ck.test/campaigns/c9/send" in text
        assert "source: ck_send (ck)" in text
        assert "X-Dry-Run: true" in text
        assert '"subject": "Hello"' in text
        assert "hunter2-secret" not in text and "[redacted]" in text
        assert TOKEN not in text and "Authorization" not in text
        assert listener.forgotten == [q["question_id"]]
        (sent,) = upstream.requests
        assert sent.headers["x-dry-run"] == "true"
        assert len(broker._runs["sess-0001"].grants) == 0
    finally:
        broker.close()


def test_write_request_declined_is_not_sent(home):
    _store(home)
    listener = FakeListener(["deny"])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"}, json={})
        assert reply["ok"] is False
        assert reply["result"]["status"] == "approval_declined"
        assert upstream.requests == []
    finally:
        broker.close()


def test_write_request_timeout_is_not_sent(home):
    _store(home)
    listener = FakeListener([None])
    broker, binding, upstream = _broker(home, listener=listener, approval_timeout_s=0.3)
    try:
        start = time.monotonic()
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"}, json={})
        assert time.monotonic() - start < 5
        assert reply["result"]["status"] == "approval_timeout"
        assert upstream.requests == []
        assert listener.forgotten == [listener.asked[0]["question_id"]]
    finally:
        broker.close()


def test_grant_bound_to_hash_single_use_and_ttl():
    table = GrantTable()
    g = table.mint("h1", now=100.0)
    assert table.consume(g, "h2", now=101.0) is False
    assert table.consume(g, "h1", now=101.0) is False  # a wrong hash spent it
    g = table.mint("h1", now=100.0)
    assert table.consume(g, "h1", now=101.0) is True
    assert table.consume(g, "h1", now=101.0) is False
    g = table.mint("h1", now=100.0)
    assert table.consume(g, "h1", now=100.0 + cb.GRANT_TTL_S + 1) is False
    assert table.consume("missing", "h1") is False
    table.mint("h1")
    table.clear()
    assert len(table) == 0


def test_two_identical_always_approve_requests_get_two_questions_and_grants(home):
    _store(home)
    listener = FakeListener(["approve", "approve"])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        for _ in range(2):
            assert _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"}, json={"a": 1})["ok"]
        assert len(listener.asked) == 2
        assert listener.asked[0]["question_id"] != listener.asked[1]["question_id"]
        assert len(upstream.requests) == 2
    finally:
        broker.close()


def test_approval_waits_then_send_uses_the_same_prepared_request(home):
    store = _store(home)

    def rotate_then_approve(question):
        store.upsert(provider="ck", handle="acme", access_token=NEW_TOKEN)
        return "approve"

    listener = FakeListener([rotate_then_approve])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"}, json={"subject": "Hi"})
        assert reply["ok"] is True
        (sent,) = upstream.requests
        assert sent.headers["authorization"] == f"Bearer {NEW_TOKEN}"
        assert str(sent.url) == "https://api.ck.test/campaigns/c9/send"
        assert json.loads(sent.content) == {"subject": "Hi"}
        assert NEW_TOKEN not in json.dumps(reply) and TOKEN not in json.dumps(reply)
    finally:
        broker.close()


def test_revoked_row_fails_closed_without_env_fallback(home, monkeypatch):
    store = _store(home)
    monkeypatch.setenv("FABRI_CRED_CK_ACME", "env-token-should-not-be-used")
    broker, binding, upstream = _broker(home)
    try:
        store.set_status("ck", "acme", "revoked")
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
        assert reply["ok"] is False
        assert reply["result"]["status"] == "credential_unavailable"
        assert reply["error"] == "ck_get: the stored ck credential is revoked; run fabri connect ck"
        assert upstream.requests == []

        # A present row whose token is dead refuses too, without the env.
        store.upsert(provider="ck", handle="acme", access_token=TOKEN, expires_in=10, now=time.time() - 3600)
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
        assert reply["result"]["status"] == "credential_unavailable"
        assert "expired" in reply["error"]
        assert upstream.requests == []

        # With no row at all the operator's env var is used.
        store.delete("ck", "acme")
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
        assert reply["ok"] is True
        assert upstream.requests[-1].headers["authorization"] == "Bearer env-token-should-not-be-used"
        assert "env-token-should-not-be-used" not in json.dumps(reply)

        monkeypatch.delenv("FABRI_CRED_CK_ACME")
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
        assert reply["result"]["status"] == "credential_unavailable"
        assert "run fabri connect ck" in reply["error"]
    finally:
        broker.close()


def test_one_get_token_per_call(home, monkeypatch):
    _store(home)
    calls = []
    real = ConnectionStore.get_token

    def counting(self, provider, handle, **kw):
        calls.append((provider, handle))
        return real(self, provider, handle, **kw)

    monkeypatch.setattr(ConnectionStore, "get_token", counting)
    broker, binding, _ = _broker(home)
    try:
        assert _http(broker, binding, "ck_get", path_params={"list_id": "1"})["ok"]
        assert calls == [("ck", "acme")]
    finally:
        broker.close()


def test_user_row_of_another_actor_is_refused(home):
    sources = [_http_decl("ck_user", "https://api.ck.test/me",
                          headers={"Authorization": {"secret_ref": "ck:u1", "prefix": "Bearer "}})]
    _store(home, handle="u1", scope_kind="user")
    broker, binding, upstream = _broker(home, sources=sources, actor="u2")
    try:
        reply = _http(broker, binding, "ck_user")
        assert reply["result"]["status"] == "credential_unavailable"
        assert reply["error"] == "ck_user: the ck credential belongs to another user"
        assert upstream.requests == []
    finally:
        broker.close()
    broker, binding, upstream = _broker(home, sources=sources, actor="u1", sid="sess-0002")
    try:
        assert _http(broker, binding, "ck_user")["ok"] is True
    finally:
        broker.close()


def test_catalog_floor_applies_to_rendered_url(home):
    sources = [_http_decl("slack_post", "https://slack.com/api/chat.postMessage",
                          headers={"Authorization": {"secret_ref": "slack:acme", "prefix": "Bearer "}})]
    broker, binding, upstream = _broker(home, sources=sources, listener=None)
    try:
        assert binding.sources["slack_post"].tools["slack_post"].method == "GET"
        reply = _http(broker, binding, "slack_post")
        assert reply["result"]["status"] == "approval_required"
        assert reply["error"] == ("slack_post needs human approval and this run has no approver; "
                                  "the call was not made")
        assert upstream.requests == []
        source, tb, prepared, extra, cls = broker._check_http(
            binding, {"source": "slack_post", "tool": "slack_post"})
        assert cls == "always_approve"
    finally:
        broker.close()


def test_floor_drift_is_refused(home):
    sources = [_http_decl("ck_list", "https://api.cakemail.dev/lists/{list_id}")]
    _store(home)
    broker, binding, upstream = _broker(home, sources=sources)
    try:
        reply = _http(broker, binding, "ck_list", path_params={"list_id": "-x"})
        assert reply["result"]["status"] == "refused"
        assert "outside what this tool was approved for" in reply["error"]
        assert upstream.requests == []
        assert _http(broker, binding, "ck_list", path_params={"list_id": "42"})["ok"] is True
    finally:
        broker.close()


def test_ssrf_private_host_is_refused(home):
    sources = [_http_decl("inner", "https://private.test/x")]
    _store(home)
    broker, binding, upstream = _broker(home, sources=sources)
    try:
        reply = _http(broker, binding, "inner")
        assert reply["result"]["status"] == "refused"
        assert reply["error"].startswith("inner: request refused: ")
        assert "private" in reply["error"]
        assert upstream.requests == []
    finally:
        broker.close()


def test_extra_headers_validated_and_deny_list(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        bad = [
            {"X-Note": "a\r\nInjected: 1"},
            {"Authorization": "Bearer mine"},
            {"authorization": "Bearer mine"},
            {"Host": "evil.test"},
            {f"X-H{i}": "v" for i in range(33)},
            {"X-Bad Name": "v"},
            "not-a-mapping",
        ]
        for headers in bad:
            reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"}, extra_headers=headers)
            assert reply["result"]["status"] == "refused", headers
        assert upstream.requests == []
        ok = _http(broker, binding, "ck_get", path_params={"list_id": "1"}, extra_headers={"X-Dry-Run": "1"})
        assert ok["ok"] is True
        assert upstream.requests[0].headers["x-dry-run"] == "1"
    finally:
        broker.close()


def test_oversize_request_line_is_refused(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        reply = _call_socket(binding.socket_path, b"x" * (cb.MAX_REQUEST_BYTES + 10), timeout=30)
        parsed = json.loads(reply)
        assert parsed["result"]["status"] == "refused"
        assert "one line" in parsed["error"]
        direct = broker._handle_line(binding, b"x" * (cb.MAX_REQUEST_BYTES + 1))
        assert direct["result"]["status"] == "refused"
        assert upstream.requests == []
    finally:
        broker.close()


def test_request_over_the_socket_round_trip(home):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        payload = _line(binding, op="http", source="ck_get", tool="ck_get", path_params={"list_id": "7"})
        reply = json.loads(_call_socket(binding.socket_path, payload + b"\n"))
        assert reply["ok"] is True and reply["v"] == 1
        assert upstream.requests[0].url.path == "/lists/7/contacts"
        assert TOKEN not in json.dumps(reply)
    finally:
        broker.close()


# ---------------------------------------------------------------- mcp


GH_URL = "https://mcp.gh.test/mcp"
GH_SOURCE = {"kind": "mcp", "name": "gh", "url": GH_URL,
             "headers": {"Authorization": {"secret_ref": "gh:acme", "prefix": "Bearer "}}}
GH_TOOLS = [
    {"name": "get_issue", "description": "Read an issue", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object"}},
    {"name": "delete_repo", "description": "Delete a repository", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object"}},
]


def _mcp_upstream(big: bytes | None = None) -> Upstream:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if big is not None:
            return httpx.Response(200, content=big, headers={"content-type": "application/json"})
        if body["method"] == "tools/list":
            result = {"tools": GH_TOOLS}
        else:
            result = {"echo": request.headers.get("authorization")}
        sse = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': result})}\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream",
                                                                   "mcp-session-id": "s-1",
                                                                   "x-other": "hidden"})

    return Upstream(respond)


def _mcp(broker, binding, body, **fields):
    raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
    return broker._handle_line(binding, _line(binding, op="mcp", source="gh", url="https://ignored.test/",
                                              body_b64=base64.b64encode(raw).decode(),
                                              extra_headers={"Accept": "application/json, text/event-stream",
                                                             "Content-Type": "application/json",
                                                             "Mcp-Session-Id": "s-1"}, **fields))


def _gh_store(home):
    ConnectionStore(home / "installs.db").upsert(provider="gh", handle="acme", access_token=GH_TOKEN)


def test_mcp_reads_pass_tools_call_needs_grant_and_list_cache_classifies(home):
    _gh_store(home)
    listener = FakeListener(["approve"])
    broker, binding, upstream = _broker(home, _mcp_upstream(), listener=listener, sources=[GH_SOURCE])
    try:
        assert binding.sources["gh"].kind == "mcp" and binding.sources["gh"].url == GH_URL
        init = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["ok"] is True and init["status"] == 200
        assert init["headers"] == {"content-type": "text/event-stream", "mcp-session-id": "s-1"}
        body = base64.b64decode(init["body_b64"])
        assert GH_TOKEN.encode() not in body and b"[redacted]" in body
        assert upstream.requests[0].headers["authorization"] == f"Bearer {GH_TOKEN}"
        assert str(upstream.requests[0].url) == GH_URL

        call_read = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "get_issue", "arguments": {"n": 1}}}
        # Before any tools/list was relayed the broker cannot classify the tool.
        listener.answers = ["deny"]
        before = _mcp(broker, binding, call_read)
        assert before["result"]["status"] == "approval_declined"
        assert len(upstream.requests) == 1

        listed = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert listed["ok"] is True
        asked = len(listener.asked)
        after = _mcp(broker, binding, call_read)
        assert after["ok"] is True
        assert len(listener.asked) == asked  # write class, no question

        listener.answers = ["approve"]
        delete = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "delete_repo", "arguments": {}}})
        assert delete["ok"] is True
        assert "gh tools/call delete_repo needs your approval" in listener.asked[-1]["question"]
        assert GH_TOKEN not in listener.asked[-1]["question"]

        for bad in ([{"jsonrpc": "2.0", "method": "ping"}], {"method": "ping"}, b"not json"):
            reply = _mcp(broker, binding, bad)
            assert reply["result"]["status"] == "refused"
        # Any other method is always_approve, so it asks.
        listener.answers = ["deny"]
        asked = len(listener.asked)
        unknown = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 9, "method": "sampling/createMessage"})
        assert unknown["result"]["status"] == "approval_declined"
        assert len(listener.asked) == asked + 1
    finally:
        broker.close()


def test_mcp_tools_cache_dropped_on_release(home):
    _gh_store(home)
    broker, binding, _ = _broker(home, _mcp_upstream(), sources=[GH_SOURCE])
    run = broker._runs["sess-0001"]
    _mcp(broker, binding, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert set(run.mcp_tools["gh"]) == {"get_issue", "delete_repo"}
    broker.release("sess-0001")
    assert run.mcp_tools == {}


def test_mcp_raw_body_cut_at_cap(home, monkeypatch):
    monkeypatch.setattr(cb, "MAX_RAW_BODY_BYTES", 1000)
    _gh_store(home)
    big = (b'{"x": "' + b"a" * 990 + GH_TOKEN.encode() + b"b" * 3000 + b'"}')
    broker, binding, _ = _broker(home, _mcp_upstream(big), sources=[GH_SOURCE])
    try:
        reply = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert reply["ok"] is True and reply["truncated"] is True
        body = base64.b64decode(reply["body_b64"])
        assert len(body) == 1000
        assert GH_TOKEN[:6].encode() not in body
    finally:
        broker.close()


# ---------------------------------------------------------------- concurrency and lifecycle


def test_concurrency_slot_limit_and_release_unblocks(home):
    _store(home)
    gate = threading.Event()
    entered = []

    def respond(request):
        entered.append(request)
        gate.wait(10)
        return httpx.Response(200, json={})

    broker, binding, _ = _broker(home, Upstream(respond))
    replies: list[dict] = []

    def call():
        replies.append(_http(broker, binding, "ck_get", path_params={"list_id": "1"}))

    threads = [threading.Thread(target=call) for _ in range(cb.MAX_INFLIGHT_PER_RUN + 1)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while len(entered) < cb.MAX_INFLIGHT_PER_RUN and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    assert len(entered) == cb.MAX_INFLIGHT_PER_RUN  # the ninth waits for a slot
    broker.release("sess-0001")
    gate.set()
    for t in threads:
        t.join(5)
    assert len(replies) == cb.MAX_INFLIGHT_PER_RUN + 1
    refused = [r for r in replies if not r.get("ok")]
    assert len(entered) == cb.MAX_INFLIGHT_PER_RUN
    assert len(refused) >= 1


def test_approval_waits_do_not_hold_send_slots(home):
    _store(home)
    listener = FakeListener([None] * cb.MAX_INFLIGHT_PER_RUN)
    broker, binding, upstream = _broker(home, listener=listener, approval_timeout_s=30)
    replies: list[dict] = []
    threads = [threading.Thread(target=lambda: replies.append(
        _http(broker, binding, "ck_send", path_params={"campaign_id": "c"}, json={})))
        for _ in range(cb.MAX_INFLIGHT_PER_RUN)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while len(listener.asked) < cb.MAX_INFLIGHT_PER_RUN and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(listener.asked) == cb.MAX_INFLIGHT_PER_RUN
    start = time.monotonic()
    read = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
    assert read["ok"] is True and time.monotonic() - start < 3
    # Release wakes every parked approval promptly.
    start = time.monotonic()
    broker.release("sess-0001")
    for t in threads:
        t.join(5)
    assert time.monotonic() - start < 3
    assert [r["result"]["status"] for r in replies] == ["approval_timeout"] * cb.MAX_INFLIGHT_PER_RUN
    assert len(upstream.requests) == 1


def test_client_gone_before_send_drops_the_grant(home):
    _store(home)
    client_end, server_end = socket.socketpair()

    def hang_up_then_approve(question):
        client_end.close()
        return "approve"

    listener = FakeListener([hang_up_then_approve])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = broker._handle_line(binding, _line(binding, op="http", source="ck_send", tool="ck_send",
                                                   path_params={"campaign_id": "c"}, json={}), conn=server_end)
        assert reply["result"]["status"] == "refused"
        assert "went away" in reply["error"]
        assert upstream.requests == []
        assert len(broker._runs["sess-0001"].grants) == 0
    finally:
        server_end.close()
        broker.close()


def test_audit_lines_have_no_values_or_queries(home):
    _store(home)
    audit = home / "audit.jsonl"
    listener = FakeListener(["deny"])
    broker, binding, _ = _broker(home, listener=listener, audit_path=audit)
    try:
        _http(broker, binding, "ck_get", path_params={"list_id": "1"}, params={"q": "query-secret-value"})
        _http(broker, binding, "ck_send", path_params={"campaign_id": "c"}, json={"x": "body-value"})
        broker._handle_line(binding, json.dumps({"v": 1, "key": "wrong"}).encode())
    finally:
        broker.close()
    text = audit.read_text()
    for forbidden in (TOKEN, "query-secret-value", "body-value", "?q=", binding.key, "Bearer"):
        assert forbidden not in text
    decisions = [json.loads(line) for line in text.splitlines() if '"decision"' in line]
    assert [d["decision"] for d in decisions] == ["sent", "declined", "refused"]
    sent = decisions[0]
    assert {k: sent[k] for k in ("session", "source", "tool", "method", "host", "path", "class", "code")} == {
        "session": "sess-0001", "source": "ck_get", "tool": "ck_get", "method": "GET", "host": "api.ck.test",
        "path": "/lists/1/contacts", "class": "read", "code": 200}


def test_no_listener_refuses_non_read(home):
    _store(home)
    broker, binding, upstream = _broker(home, listener=None)
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c"}, json={})
        assert reply == {"v": 1, "ok": False,
                         "error": "ck_send needs human approval and this run has no approver; the call was not made",
                         "result": {"status": "approval_required", "code": None}}
        assert upstream.requests == []
    finally:
        broker.close()


def test_peer_uid_mismatch_closes(home, monkeypatch):
    _store(home)
    broker, binding, upstream = _broker(home)
    try:
        monkeypatch.setattr(cb, "peer_uid", lambda conn: os.getuid() + 1)
        payload = _line(binding, op="http", source="ck_get", tool="ck_get", path_params={"list_id": "1"})
        assert _call_socket(binding.socket_path, payload + b"\n") == b""
        assert upstream.requests == []
    finally:
        broker.close()


def test_peer_outside_the_run_tree_closes(home):
    _store(home)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        broker, binding, upstream = _broker(home)
        try:
            broker.attach_run_pid("sess-0001", stranger.pid)
            payload = _line(binding, op="http", source="ck_get", tool="ck_get", path_params={"list_id": "1"})
            assert _call_socket(binding.socket_path, payload + b"\n") == b""
            assert upstream.requests == []
            # The run's own tree (here: this test process) is let in.
            broker.attach_run_pid("sess-0001", os.getpid())
            assert json.loads(_call_socket(binding.socket_path, payload + b"\n"))["ok"] is True
        finally:
            broker.close()
    finally:
        stranger.kill()
        stranger.wait()
    assert cb.is_descendant(os.getpid(), os.getppid()) is True
    assert cb.is_descendant(os.getppid(), os.getpid()) is False


def test_release_unlinks_socket_and_clears_grants(home):
    _store(home)
    broker, binding, _ = _broker(home)
    run = broker._runs["sess-0001"]
    run.grants.mint("abc")
    sock = Path(binding.socket_path)
    assert sock.exists()
    broker.release("sess-0001")
    assert not sock.exists()
    assert len(run.grants) == 0
    assert broker.bindings() == {}
    broker.release("sess-0001")
    broker.release("never-bound")
    reply = _http(broker, binding, "ck_get", path_params={"list_id": "1"})
    assert reply["result"]["status"] == "refused"


def test_approval_round_trip_through_real_listener(home):
    _store(home)
    questions: list[dict] = []
    listener = AskUserListener(str(home / "ask.sock"), questions.append)
    broker, binding, upstream = _broker(home, listener=listener)

    def answer():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = listener.pending_questions()
            if pending:
                assert pending[0]["kind"] == "approval"
                listener.answer(pending[0]["question_id"], "approve", selected_option="approve")
                return
            time.sleep(0.02)

    t = threading.Thread(target=answer)
    t.start()
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c1"}, json={"subject": "s"})
        t.join(5)
        assert reply["ok"] is True
        assert len(upstream.requests) == 1
        assert questions and questions[0]["kind"] == "approval"
        assert TOKEN not in json.dumps(questions)
        assert listener.pending_questions() == []
    finally:
        broker.close()
        listener.close()


# ---------------------------------------------------------------- duplicate bindings (finding 2)


def _parent_child(home, parent_sources, child_sources, parent_policy=None, child_policy=None):
    child = _write_config(home, child_sources, child_policy, name="child.yaml")
    return _write_config(home, parent_sources, parent_policy, agents=[{"name": "helper", "config": str(child)}],
                         name="parent.yaml")


def _bind_config(home, config, upstream, listener, sid="sess-dup1"):
    broker = CredentialBroker(install_db=home / "installs.db", transport=upstream.transport)
    binding = broker.bind(session_id=sid, config_path=config, run_home=home / "runs" / sid[:8], actor=None,
                          listener=listener, run_pid=os.getpid())
    return broker, binding


CK_CREATE = "https://api.ck.test/lists/{list_id}/contacts"


@pytest.mark.parametrize("child_first", [False, True])
def test_duplicate_http_source_uses_the_strictest_class(home, child_first):
    loose = [_http_decl("ck_create", CK_CREATE, "POST")]
    strict = [_http_decl("ck_create", CK_CREATE, "POST", approval="always_approve")]
    parent, child = (strict, loose) if child_first else (loose, strict)
    config = _parent_child(home, parent, child)
    bound = bind_sources(yaml.safe_load(config.read_text()), actor=None)
    assert bound["ck_create"].tools["ck_create"].approval == "always_approve"

    _store(home)
    listener = FakeListener(["deny"])
    upstream = Upstream()
    broker, binding = _bind_config(home, config, upstream, listener)
    try:
        reply = _http(broker, binding, "ck_create", path_params={"list_id": "L1"}, json={"email": "a@b.test"})
        assert reply["result"]["status"] == "approval_declined"
        assert len(listener.asked) == 1 and upstream.requests == []
    finally:
        broker.close()


def test_duplicate_http_source_keeps_a_child_policy_floor(home):
    decl = [_http_decl("ck_create", CK_CREATE, "POST")]
    floored = {**POLICY, "approval_floor": {"ck_*": "always_approve"}}
    config = _parent_child(home, decl, decl, child_policy=floored)
    bound = bind_sources(yaml.safe_load(config.read_text()), actor=None)
    src = bound["ck_create"]
    assert src.tools["ck_create"].approval == "always_approve"
    assert ("ck_*", "always_approve") in src.floors


def test_duplicate_source_with_different_shape_is_still_refused(home):
    config = _parent_child(home, [_http_decl("ck_create", CK_CREATE, "POST")],
                           [_http_decl("ck_create", CK_CREATE, "PUT")])
    with pytest.raises(BindError, match="ck_create"):
        bind_sources(yaml.safe_load(config.read_text()), actor=None)


@pytest.mark.parametrize("how", ["approval", "floor"])
def test_duplicate_mcp_source_applies_every_declaration(home, how):
    child_source, child_policy = dict(GH_SOURCE), None
    if how == "approval":
        child_source["approval"] = {"mcp_gh_get_issue": "always_approve"}
    else:
        child_policy = {**POLICY, "approval_floor": {"mcp_gh_*": "always_approve"}}
    config = _parent_child(home, [GH_SOURCE], [child_source], child_policy=child_policy)
    bound = bind_sources(yaml.safe_load(config.read_text()), actor=None)["gh"]
    if how == "approval":
        assert {"mcp_gh_get_issue": "always_approve"} in bound.all_approval_decls()
    else:
        assert ("mcp_gh_*", "always_approve") in bound.floors

    _gh_store(home)
    listener = FakeListener(["deny"])
    upstream = _mcp_upstream()
    broker, binding = _bind_config(home, config, upstream, listener)
    try:
        assert _mcp(broker, binding, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["ok"] is True
        sent = len(upstream.requests)
        reply = _mcp(broker, binding, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "get_issue", "arguments": {"n": 1}}})
        # Alone the parent would send this as write without a question.
        assert reply["result"]["status"] == "approval_declined"
        assert len(listener.asked) == 1 and len(upstream.requests) == sent
    finally:
        broker.close()


# ---------------------------------------------------------------- exact approval cards (finding 3)


def test_card_lists_query_pairs_in_order_with_repeats(home):
    request = httpx.Request("GET", "https://api.ck.test/x",
                            params=[("role", "admin"), ("role", "viewer"), ("api_key", "s3cr3t-value")])
    text = cb.describe_request("t", "s", "always_approve", None, request, {}, exact=True)
    assert 'query: [["role", "admin"], ["role", "viewer"], ["api_key", "[redacted]"]]' in text
    assert "s3cr3t-value" not in text


def test_always_approve_card_shows_both_list_params(home):
    _store(home)
    listener = FakeListener(["approve"])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"},
                      params={"role": ["admin", "viewer"]}, json={"subject": "Hi"})
        assert reply["ok"] is True
        assert '[["role", "admin"], ["role", "viewer"]]' in listener.asked[0]["question"]
        (sent,) = upstream.requests
        assert sent.url.query == b"role=admin&role=viewer"
    finally:
        broker.close()


def test_always_approve_body_over_the_card_cap_is_refused_unasked(home):
    _store(home)
    listener = FakeListener(["approve"])
    broker, binding, upstream = _broker(home, listener=listener)
    try:
        big = {"text": "x" * (cb.APPROVAL_CARD_BODY_CAP + 10)}
        reply = _http(broker, binding, "ck_send", path_params={"campaign_id": "c9"}, json=big)
        assert reply["ok"] is False and reply["result"]["status"] == "refused"
        assert "cannot show it exactly" in reply["error"] and "the call was not made" in reply["error"]
        assert listener.asked == [] and upstream.requests == []
        # A write call needs no card, so the same body still goes.
        assert _http(broker, binding, "ck_create", path_params={"list_id": "L1"}, json=big)["ok"] is True
    finally:
        broker.close()


def test_card_refuses_bodies_it_cannot_show_exactly():
    raw = httpx.Request("POST", "https://api.ck.test/x", content=b"\x00\x01 raw bytes")
    with pytest.raises(cb.RequestNotShowable, match="not JSON or form"):
        cb.describe_request("t", "s", "always_approve", None, raw, {}, exact=True)
    dup = httpx.Request("POST", "https://api.ck.test/x", content=b'{"a": 1, "a": 2}')
    with pytest.raises(cb.RequestNotShowable, match="repeats a key"):
        cb.describe_request("t", "s", "always_approve", None, dup, {}, exact=True)
    form = httpx.Request("POST", "https://api.ck.test/x", data={"to": ["a", "b"]})
    text = cb.describe_request("t", "s", "always_approve", None, form, {}, exact=True)
    assert '["to", "a"]' in text and '["to", "b"]' in text
    # Without exact the card still summarises, for callers that only display.
    assert "not JSON" in cb.describe_request("t", "s", "always_approve", None, raw, {})


def test_mcp_body_with_a_repeated_key_is_refused(home):
    _gh_store(home)
    listener = FakeListener(["approve"])
    broker, binding, upstream = _broker(home, _mcp_upstream(), listener=listener, sources=[GH_SOURCE])
    try:
        raw = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "method": "ping"}'
        reply = _mcp(broker, binding, raw)
        assert reply["result"]["status"] == "refused" and "repeats a JSON key" in reply["error"]
        assert listener.asked == [] and upstream.requests == []
    finally:
        broker.close()


# ---------------------------------------------------------------- pinned outbound client (finding 5)


def test_default_client_is_pinned_and_ignores_proxy_env(home, monkeypatch):
    from fabri.tools.security.ssrf import PinnedTransport

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.test:3128")
    broker = CredentialBroker(install_db=home / "installs.db")
    config = _write_config(home)
    broker.bind(session_id="sess-pin1", config_path=config, run_home=home / "runs" / "pin1", actor=None,
                listener=None, run_pid=os.getpid())
    try:
        client = broker._runs["sess-pin1"].client
        assert isinstance(client._transport, PinnedTransport)
        assert not client._mounts
    finally:
        broker.close()


def test_dns_rebinding_after_the_prechecks_is_refused_at_connect(home, monkeypatch):
    """The broker's two prechecks see a public answer; the connect's own
    lookup sees a private one. Nothing reaches the network."""
    from fabri.tools.security.ssrf import PinnedTransport

    answers = iter(["93.184.216.34", "93.184.216.34", "10.0.0.9"])

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port or 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)
    connects: list[str] = []

    class NoNetwork:
        def connect_tcp(self, host, *args, **kwargs):  # pragma: no cover -- must not be reached
            connects.append(host)
            raise AssertionError("connected")

        def sleep(self, seconds):  # pragma: no cover
            pass

    _store(home)
    upstream = SimpleNamespace(transport=PinnedTransport(inner_backend=NoNetwork()))
    broker, binding, _ = _broker(home, upstream=upstream)
    try:
        reply = _http(broker, binding, "ck_get", path_params={"list_id": "L1"})
        assert reply["ok"] is False and "ConnectError" in reply["error"]
        assert connects == []
        # All three lookups happened: both prechecks and the connect's own.
        assert next(answers, None) is None
        assert TOKEN not in json.dumps(reply)
    finally:
        broker.close()
