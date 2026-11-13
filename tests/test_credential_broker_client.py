"""Tests for the run-side credential broker client.

A tiny threaded fake broker listens on a short temp path, records each
request line and answers with a canned reply.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import time

import pytest

from fabri.tools import credential_broker_client as cbc
from fabri.tools.credential_broker_client import (
    BROKER_ENV_NAMES,
    KEY_ENV,
    SOCKET_ENV,
    BrokerClient,
    BrokerUnavailable,
)

KEY = "k" * 20 + "SECRETCAPABILITY" + "k" * 28


class FakeBroker:
    """Accepts connections one at a time. Each connection reads one line,
    records it, notes whether the client had already half closed, then
    writes the next canned reply (bytes) or does nothing (None keeps the
    connection open until stop)."""

    def __init__(self, replies: list[bytes | None]) -> None:
        self.dir = tempfile.mkdtemp(prefix="fabri-bc-")
        self.path = os.path.join(self.dir, "b.sock")
        assert len(self.path.encode()) < 104
        self.replies = list(replies)
        self.lines: list[bytes] = []
        self.client_half_closed: list[bool] = []
        self.connections = 0
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._srv.settimeout(0.1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            with conn:
                conn.settimeout(5)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                self.lines.append(buf)
                # The broker peeks before sending an approved request; end of
                # file there means the caller left.
                # A socket with a timeout waits inside recv even with
                # MSG_DONTWAIT, so peek in non-blocking mode.
                conn.setblocking(False)
                try:
                    peek = conn.recv(1, socket.MSG_PEEK)
                    self.client_half_closed.append(peek == b"")
                except BlockingIOError:
                    self.client_half_closed.append(False)
                conn.settimeout(5)
                reply = self.replies.pop(0) if self.replies else None
                if reply is None:
                    self._stop.wait(10)
                    continue
                try:
                    conn.sendall(reply)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def fake():
    made: list[FakeBroker] = []

    def make(*replies: bytes | None) -> FakeBroker:
        fb = FakeBroker(list(replies))
        made.append(fb)
        return fb

    yield make
    for fb in made:
        fb.stop()


def line(obj: dict) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def client(fb: FakeBroker, **kw) -> BrokerClient:
    kw.setdefault("approval_timeout_s", 5.0)
    return BrokerClient(fb.path, KEY, **kw)


OK_REPLY = {"v": 1, "ok": True, "result": {"status": "ok", "code": 201, "data": {"id": 7}}}


def test_from_env_needs_both_vars(caplog):
    caplog.set_level(logging.DEBUG)
    assert BROKER_ENV_NAMES == {SOCKET_ENV, KEY_ENV}
    assert all(n.startswith("FABRI_CRED_") for n in BROKER_ENV_NAMES)
    assert BrokerClient.from_env({}) is None
    assert BrokerClient.from_env({SOCKET_ENV: "/tmp/x.sock"}) is None
    assert BrokerClient.from_env({KEY_ENV: KEY}) is None
    assert BrokerClient.from_env({SOCKET_ENV: "", KEY_ENV: KEY}) is None
    assert caplog.records == []
    c = BrokerClient.from_env({SOCKET_ENV: "/tmp/x.sock", KEY_ENV: KEY})
    assert isinstance(c, BrokerClient)
    assert c.socket_path == "/tmp/x.sock"


def test_from_env_reads_approval_timeout(monkeypatch):
    monkeypatch.setenv("FABRI_APPROVAL_TIMEOUT_S", "42")
    c = BrokerClient.from_env({SOCKET_ENV: "/tmp/x.sock", KEY_ENV: KEY})
    assert c.approval_timeout_s == 42.0
    monkeypatch.setenv("FABRI_APPROVAL_TIMEOUT_S", "nope")
    assert BrokerClient("/tmp/x.sock", KEY).approval_timeout_s == 300.0


def test_send_writes_one_line_with_key_and_shape(fake):
    fb = fake(line(OK_REPLY), line(OK_REPLY))
    c = client(fb)
    send = c.sender("ck", "ck_contacts_create")
    out = send("post", "https://ignored.example/lists/42/contacts",
               path_params={"list_id": 42}, json_body={"email": "a@b.c"},
               extra_headers={"X-Dry-Run": "true"}, timeout_s=12, floor_url="https://ignored.example/x")
    assert out["ok"] is True
    assert len(fb.lines) == 1
    raw = fb.lines[0]
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    req = json.loads(raw)
    assert req == {
        "v": 1, "key": KEY, "op": "http", "source": "ck", "tool": "ck_contacts_create",
        "method": "POST", "path_params": {"list_id": 42}, "params": None,
        "json": {"email": "a@b.c"}, "form": None, "extra_headers": {"X-Dry-Run": "true"},
        "timeout_s": 12.0,
    }
    # No rendered URL, no floor URL and no header specs ever leave the run.
    assert "url" not in req and "floor_url" not in req and "headers" not in req
    # The client keeps its write side open while it waits.
    assert fb.client_half_closed == [False]

    # A sender without a bound tool takes the tool from tool_name.
    send2 = c.sender("ck")
    send2("GET", "", params={"q": "x"}, tool_name="ck_contacts_list")
    req2 = json.loads(fb.lines[1])
    assert req2["tool"] == "ck_contacts_list"
    assert req2["params"] == {"q": "x"} and req2["json"] is None and req2["path_params"] == {}


def test_send_both_bodies_raises_before_io(fake):
    fb = fake(line(OK_REPLY))
    c = client(fb)
    with pytest.raises(ValueError):
        c.send(source="ck", tool="t", method="POST", json_body={"a": 1}, form_body={"b": 2})
    with pytest.raises(ValueError):
        c.sender("ck", "t")("POST", "", json_body={"a": 1}, form_body={"b": 2})
    time.sleep(0.2)
    assert fb.connections == 0


def test_send_returns_reply_dict_untouched(fake):
    reply = {"v": 1, "ok": False, "error": "HTTP 404",
             "result": {"status": "error", "code": 404, "data": {"msg": "[redacted]"}}, "extra": [1, 2]}
    fb = fake(line(reply))
    out = client(fb).send(source="ck", tool="t", method="GET")
    assert out == reply


def test_send_maps_transport_errors_to_tool_error_without_key(fake):
    def check(out: dict, reason: str) -> None:
        assert out["ok"] is False
        assert out["error"].startswith("t: credential broker unreachable (")
        assert reason in out["error"]
        assert out["result"] == {"status": "error", "code": None}
        assert KEY not in json.dumps(out)

    # Socket missing.
    d = tempfile.mkdtemp(prefix="fabri-bc-")
    try:
        gone = BrokerClient(os.path.join(d, "none.sock"), KEY, approval_timeout_s=1)
        check(gone.send(source="ck", tool="t", method="GET"), "FileNotFoundError")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # Closed without a reply, a non-JSON reply, a wrong version, a non-object, no ok flag.
    fb = fake(b"", b"not json\n", line({"v": 2, "ok": True}), b"[1]\n", line({"v": 1}))
    c = client(fb)
    check(c.send(source="ck", tool="t", method="GET"), "connection closed")
    check(c.send(source="ck", tool="t", method="GET"), "not JSON")
    check(c.send(source="ck", tool="t", method="GET"), "protocol version")
    check(c.send(source="ck", tool="t", method="GET"), "not a JSON object")
    check(c.send(source="ck", tool="t", method="GET"), "no ok flag")

    # An argument that cannot be encoded never reaches the socket.
    before = fb.connections
    check(c.send(source="ck", tool="t", method="POST", json_body={"a": object()}), "could not be encoded")
    # A request line over the broker cap is refused locally.
    check(c.send(source="ck", tool="t", method="POST", json_body={"a": "x" * cbc.MAX_REQUEST_BYTES}),
          "request too large")
    assert fb.connections == before


def test_post_mcp_encodes_body_and_decodes_reply(fake):
    body_out = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
    reply = {"v": 1, "ok": True, "status": 200,
             "headers": {"content-type": "application/json", "mcp-session-id": "abc"},
             "body_b64": base64.b64encode(body_out).decode(), "truncated": False}
    fb = fake(line(reply))
    c = client(fb)
    body_in = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    status, headers, data = c.post_mcp(
        source="gh", url="https://api.example.test/mcp/", body=body_in,
        headers={"Mcp-Session-Id": "abc", "Accept": "application/json, text/event-stream"}, timeout_s=9,
    )
    assert (status, headers, data) == (200, {"content-type": "application/json", "mcp-session-id": "abc"}, body_out)
    req = json.loads(fb.lines[0])
    assert req == {
        "v": 1, "key": KEY, "op": "mcp", "source": "gh", "url": "https://api.example.test/mcp/",
        "extra_headers": {"Mcp-Session-Id": "abc", "Accept": "application/json, text/event-stream"},
        "body_b64": base64.b64encode(body_in).decode(), "timeout_s": 9.0,
    }
    assert fb.client_half_closed == [False]


def test_post_mcp_refusal_raises_with_text(fake):
    refusal = {"v": 1, "ok": False, "error": "gh_create_issue needs human approval and this run has no approver",
               "result": {"status": "approval_required", "code": None}}
    fb = fake(line(refusal), line({"v": 1, "ok": True, "status": 200, "headers": {}, "body_b64": "%%%"}))
    c = client(fb)
    with pytest.raises(BrokerUnavailable) as ei:
        c.post_mcp(source="gh", url="https://x.test/mcp", body=b"{}", headers={})
    assert str(ei.value) == refusal["error"]
    with pytest.raises(BrokerUnavailable, match="not base64"):
        c.post_mcp(source="gh", url="https://x.test/mcp", body=b"{}", headers={})

    d = tempfile.mkdtemp(prefix="fabri-bc-")
    try:
        gone = BrokerClient(os.path.join(d, "none.sock"), KEY, approval_timeout_s=1)
        with pytest.raises(BrokerUnavailable) as ei2:
            gone.post_mcp(source="gh", url="https://x.test/mcp", body=b"{}", headers={})
        assert str(ei2.value) == "FileNotFoundError"
        assert KEY not in repr(ei2.value)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reply_cap_and_timeout(fake, monkeypatch):
    assert cbc.MAX_RESPONSE_BYTES == 8 * 1_048_576
    # A reply with no newline before the cap is refused.
    monkeypatch.setattr(cbc, "MAX_RESPONSE_BYTES", 4096)
    fb = fake(b"x" * 10_000 + b"\n", b"{" + b" " * 5000 + b"\n")
    c = client(fb)
    out = c.send(source="ck", tool="t", method="GET")
    assert out["ok"] is False and "reply too large" in out["error"]
    with pytest.raises(BrokerUnavailable, match="reply too large"):
        c.post_mcp(source="gh", url="https://x.test/mcp", body=b"{}", headers={})

    # The wait is the send timeout plus the approval timeout plus slack.
    assert BrokerClient("/tmp/x.sock", KEY, approval_timeout_s=300)._wait_s(30) == 335.0

    # A broker that never answers times out.
    monkeypatch.setattr(cbc, "_WAIT_SLACK_S", 0.3)
    fb2 = fake(None)
    c2 = BrokerClient(fb2.path, KEY, approval_timeout_s=0.2)
    t0 = time.monotonic()
    out2 = c2.send(source="ck", tool="t", method="GET", timeout_s=0.0)
    elapsed = time.monotonic() - t0
    assert out2["ok"] is False and "TimeoutError" in out2["error"]
    assert 0.4 <= elapsed < 3.0


def test_repr_and_logs_have_no_key(fake, caplog):
    caplog.set_level(logging.DEBUG)
    d = tempfile.mkdtemp(prefix="fabri-bc-")
    try:
        path = os.path.join(d, "none.sock")
        c = BrokerClient(path, KEY, approval_timeout_s=1)
        assert KEY not in repr(c) and KEY not in str(c)
        assert path in repr(c)
        out = c.send(source="ck", tool="t", method="GET")
        with pytest.raises(BrokerUnavailable) as ei:
            c.post_mcp(source="gh", url="https://x.test/mcp", body=b"{}", headers={})
    finally:
        shutil.rmtree(d, ignore_errors=True)
    fb = fake(b"garbage\n")
    client(fb).send(source="ck", tool="t", method="GET")
    assert KEY not in json.dumps(out)
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)
    assert caplog.records, "a transport failure is logged"
    for rec in caplog.records:
        assert KEY not in rec.getMessage()
    with pytest.raises(ValueError):
        BrokerClient(path, "")
