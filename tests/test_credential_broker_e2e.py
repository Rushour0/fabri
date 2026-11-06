"""End to end: a real FabriService run whose credentialed tool calls go
through the real broker socket.

The run is a small stub agent (no model). It builds its registry with
``fabri.runtime.build_tools`` from the bound run.yaml, exactly as a real run
does, and calls tools from a script the test passes in the task. So the
path under test is the real one: registry, source handler, BrokerClient,
the per-run Unix socket, the broker in the service process, the
ConnectionStore lookup and an httpx MockTransport standing in for the
provider. Approval questions are answered through ``FabriService.answer``,
the same call the ``POST /runs/<id>/answer`` route makes.

No assertion prints a token: comparisons that involve one are reduced to
labels or booleans first.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.service.connection_store import ConnectionStore
from fabri.service.credential_broker import CredentialBroker
from fabri.service.service import FabriService

TOKEN = "tok-e2e-ORIGINAL-5c1d9a7e3b"
NEW_TOKEN = "tok-e2e-RECONNECTED-8f2a6b0d4c"
ENV_FALLBACK = "tok-e2e-SERVICE-ENV-3e7c1a9f"
EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "fabri" / "tools" / "examples"
CK_AUTH = {"Authorization": {"secret_ref": "ck:acme", "prefix": "Bearer "}}
SOURCES = [
    {"name": "ck_get", "kind": "http", "url": "https://api.ck.test/lists/{list_id}/contacts",
     "method": "GET", "headers": CK_AUTH},
    {"name": "ck_create", "kind": "http", "url": "https://api.ck.test/lists/{list_id}/contacts",
     "method": "POST", "headers": CK_AUTH},
    {"name": "ck_send", "kind": "http", "url": "https://api.ck.test/campaigns/{campaign_id}/send",
     "method": "POST", "headers": CK_AUTH},
]

# The stub agent. argv: task JSON, bound config path. The task is
# {"sync": <dir>, "steps": [...]} where a step is one of
#   {"call": <tool>, "args": {...}}   invoke a tool through the registry
#   {"mark": <name>}                  create sync/<name>
#   {"wait": <name>}                  wait for sync/<name> (the test creates it)
#   {"raw": <name>}                   send one broker line, with this run's key,
#                                     to the socket path written in sync/<name>
# It writes its full env to sync/run_env.json for the test to search, and
# never prints its own key.
STUB_AGENT = r'''
import json, os, socket, sys, time
from pathlib import Path

task = json.loads(sys.argv[1])
config_path = sys.argv[2]
home = Path(os.environ["FABRI_HOME"])
sid = os.environ["FABRI_SESSION_ID"]
trace = home / ".fabri" / "traces" / (sid + ".jsonl")
trace.parent.mkdir(parents=True, exist_ok=True)
with trace.open("a") as f:
    f.write(json.dumps({"type": "start", "task": "stub"}) + "\n")
sync = Path(task["sync"])
(sync / "run_env.json").write_text(json.dumps(dict(os.environ)))
key = os.environ.get("FABRI_CRED_BROKER_KEY", "")

from fabri.config import load_config
from fabri.runtime import build_tools

registry = build_tools(load_config(config_path)["tools"])


def wait_for(name):
    deadline = time.monotonic() + 60
    while not (sync / name).exists():
        if time.monotonic() > deadline:
            raise SystemExit("stub agent: timed out waiting for " + name)
        time.sleep(0.02)


def raw_line(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(path)
        line = {"v": 1, "key": key, "op": "http", "source": "ck_get", "tool": "ck_get",
                "path_params": {"list_id": "1"}}
        s.sendall((json.dumps(line) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return {"reply": buf.decode("utf-8", "replace")}
    except OSError as exc:
        return {"error": type(exc).__name__}
    finally:
        s.close()


results = []
for step in task["steps"]:
    if "wait" in step:
        wait_for(step["wait"])
    elif "mark" in step:
        (sync / step["mark"]).write_text("1")
    elif "raw" in step:
        wait_for(step["raw"])
        out = raw_line((sync / step["raw"]).read_text())
        text = json.dumps(out)
        results.append({"raw": json.loads(text.replace(key, "[run key]")) if key else out})
    else:
        out = registry.invoke(step["call"], step.get("args") or {})
        text = json.dumps(out)
        seen = bool(key) and key in text
        results.append({"tool": step["call"], "key_seen": seen,
                        "out": json.loads(text.replace(key, "[run key]")) if seen else out})
print(json.dumps({"session_id": sid, "success": True, "outcome": "success",
                  "final_text": json.dumps({"results": results})}))
'''

# Run by the real bash tool inside the run. It records the env it got and
# tries the run's own broker socket (the test hands it the path) with keys
# it could guess. It never learns the real key.
PROBE = r'''
import json, os, socket, sys
from pathlib import Path

sync = Path(sys.argv[1])
(sync / "tool_env.json").write_text(json.dumps(dict(os.environ)))
path = (sync / "sock_path").read_text()
replies = []
for guess in ("", "0" * 64):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(path)
        line = {"v": 1, "key": guess, "op": "http", "source": "ck_get", "tool": "ck_get",
                "path_params": {"list_id": "1"}}
        s.sendall((json.dumps(line) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        replies.append(json.loads(buf) if buf.strip() else None)
    except OSError as exc:
        replies.append({"transport_error": type(exc).__name__})
    finally:
        s.close()
names = sorted(os.environ)
print(json.dumps({"cred_names": [n for n in names if n.startswith("FABRI_CRED_")], "replies": replies}))
'''


# ---------------------------------------------------------------- fixtures and helpers


def clean_broker_env(monkeypatch: pytest.MonkeyPatch, home_root: Path) -> None:
    """No credential, snapshot or audit knob leaks in from the outer env, and
    FABRI_INSTALL_DB (which the service sets in os.environ) is restored."""
    for name in list(os.environ):
        if name.startswith("FABRI_CRED_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("FABRI_APPROVAL_TIMEOUT_S", raising=False)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(home_root / "installs.db"))
    # The stub runs share the test's OS user, so the broker needs the local
    # development opt-in. test_same_uid_run_is_refused_without_opt_in drops it.
    monkeypatch.setenv("FABRI_ALLOW_SHARED_UID", "1")


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker checks every host against SSRF rules; resolve all of them
    to a public address so no real DNS lookup happens."""
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("fabri.tools.security.ssrf.socket.getaddrinfo", getaddrinfo)


class Upstream:
    """The provider: records every request and echoes the auth header back,
    so a response that carried the token would show it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        return httpx.Response(200, json={"echo": request.headers.get("authorization"),
                                         "path": request.url.path})

    def auth_labels(self) -> list[str]:
        """Which credential each request carried, as a label."""
        names = {f"Bearer {TOKEN}": "TOKEN", f"Bearer {NEW_TOKEN}": "NEW_TOKEN",
                 f"Bearer {ENV_FALLBACK}": "ENV_FALLBACK"}
        with self._lock:
            return [names.get(r.headers.get("authorization", ""), "other") for r in self.requests]

    def count(self) -> int:
        with self._lock:
            return len(self.requests)


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, approval_timeout_s: float = 30.0,
                 audit_path: Path | None = None, connect: bool = True) -> None:
        self.tmp = tmp_path
        self.home_root = tmp_path / "svc"
        clean_broker_env(monkeypatch, self.home_root)
        self.upstream = Upstream()
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir(exist_ok=True)
        for name in ("bash.json", "bash.py"):
            shutil.copy(EXAMPLES / name, tools_dir / name)
        (tmp_path / "probe.py").write_text(PROBE)
        template = tmp_path / "template.yaml"
        template.write_text(yaml.safe_dump({
            "llm": {"model": "m"},
            "tools": {"manifest_dir": [str(tools_dir)], "sandbox_root": str(tmp_path), "sources": SOURCES},
        }))
        script = tmp_path / "stub_agent.py"
        script.write_text(textwrap.dedent(STUB_AGENT))

        def command(task, config_path, session_id, fabri_home):
            return [sys.executable, str(script), task, str(config_path)]

        self.broker = CredentialBroker(install_db=self.home_root / "installs.db",
                                       transport=self.upstream.transport,
                                       approval_timeout_s=approval_timeout_s, audit_path=audit_path)
        self.svc = FabriService(template_config=str(template), home_root=self.home_root,
                                command_builder=command, credential_broker=self.broker)
        self.store = ConnectionStore(self.home_root / "installs.db")
        if connect:
            self.store.upsert(provider="ck", handle="acme", access_token=TOKEN)
        self._syncs = 0

    def submit(self, steps: list[dict]) -> tuple[str, Path]:
        self._syncs += 1
        sync = self.tmp / f"sync{self._syncs}"
        sync.mkdir()
        sid = self.svc.submit(json.dumps({"sync": str(sync), "steps": steps}))
        return sid, sync

    def results(self, sid: str, timeout: float = 90.0) -> list[dict]:
        out = self.svc.result(sid, timeout=timeout)
        assert out.get("success") is True, f"stub run failed: {out.get('outcome')}"
        text = out["final_text"]
        leaked = TOKEN in text or NEW_TOKEN in text or ENV_FALLBACK in text
        assert not leaked, "a token value reached the run's output"
        return json.loads(text)["results"]

    def pending_approval(self, sid: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for q in self.svc.list_pending_questions():
                if q["session_id"] == sid and q.get("kind") == "approval":
                    return q
            time.sleep(0.02)
        raise AssertionError("no approval question appeared")

    def approvals_for(self, sid: str) -> list[dict]:
        return [q for q in self.svc.list_pending_questions()
                if q["session_id"] == sid and q.get("kind") == "approval"]

    def close(self) -> None:
        self.svc.close()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    made: list[Harness] = []

    def make(**kwargs) -> Harness:
        h = Harness(tmp_path, monkeypatch, **kwargs)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def wait_file(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path.name}"
        time.sleep(0.02)


def touch(path: Path) -> None:
    path.write_text("1")


def status_of(result: dict) -> str | None:
    return ((result.get("out") or {}).get("result") or {}).get("status")


# ---------------------------------------------------------------- tests


def test_read_call_authenticates_and_child_env_has_no_value(harness):
    h = harness()
    sid, sync = h.submit([
        {"call": "ck_get", "args": {"list_id": "42"}},
        # Only always_approve needs a human: a write class call goes straight through.
        {"call": "ck_create", "args": {"list_id": "42", "email": "a@b.c"}},
    ])
    read, write = h.results(sid)

    assert read["out"]["ok"] is True, read["out"].get("error")
    assert read["out"]["result"]["code"] == 200
    # The upstream echoed the auth header; the broker redacted it before the
    # reply crossed the socket.
    assert read["out"]["result"]["data"]["echo"] == "[redacted]"
    assert read["key_seen"] is False
    assert write["out"]["ok"] is True, write["out"].get("error")

    assert h.upstream.auth_labels() == ["TOKEN", "TOKEN"]
    get, post = h.upstream.requests
    assert (get.method, get.url.path) == ("GET", "/lists/42/contacts")
    assert (post.method, post.url.path) == ("POST", "/lists/42/contacts")
    assert json.loads(post.content) == {"email": "a@b.c"}

    # No approval card for either call.
    trace = (h.home_root / sid / ".fabri" / "traces" / f"{sid}.jsonl").read_text()
    assert '"ask_user"' not in trace

    # The run process env: the two broker names, no token, no install db.
    run_env = json.loads((sync / "run_env.json").read_text())
    env_text = json.dumps(run_env)
    token_in_env = TOKEN in env_text
    assert not token_in_env, "the token reached the run env"
    assert sorted(n for n in run_env if n.startswith("FABRI_CRED_")) == [
        "FABRI_CRED_BROKER_KEY", "FABRI_CRED_BROKER_SOCKET"]
    assert "FABRI_INSTALL_DB" not in run_env
    db_path_in_env = str(h.home_root / "installs.db") in env_text
    assert not db_path_in_env, "the install db path reached the run env"


def test_write_call_shows_card_and_sends_after_answer_route(harness):
    h = harness()
    sid, _sync = h.submit([{"call": "ck_send", "args": {"campaign_id": "c9", "subject": "Hello"}}])

    q = h.pending_approval(sid)
    # Nothing was sent while the card waits.
    assert h.upstream.count() == 0
    assert q["options"] == ["approve", "deny"] and q["default"] == "deny"
    assert "ck_send needs your approval (class always_approve)" in q["question"]
    assert "POST https://api.ck.test/campaigns/c9/send" in q["question"]
    assert '"subject": "Hello"' in q["question"]
    token_on_card = TOKEN in q["question"] or "Bearer" in q["question"]
    assert not token_on_card, "the approval card shows the credential"

    h.svc.answer(sid, q["question_id"], "approve", selected_option="approve")
    (result,) = h.results(sid)

    assert result["out"]["ok"] is True, result["out"].get("error")
    assert h.upstream.auth_labels() == ["TOKEN"]
    (sent,) = h.upstream.requests
    assert (sent.method, sent.url.path) == ("POST", "/campaigns/c9/send")
    assert json.loads(sent.content) == {"subject": "Hello"}
    # The grant was used once: a repeat answer finds no question and nothing
    # more goes out.
    with pytest.raises(KeyError):
        h.svc.answer(sid, q["question_id"], "approve", selected_option="approve")
    time.sleep(0.3)
    assert h.upstream.count() == 1


def test_write_call_declined_is_not_sent(harness):
    h = harness()
    sid, _sync = h.submit([{"call": "ck_send", "args": {"campaign_id": "c9", "subject": "Hello"}}])

    q = h.pending_approval(sid)
    h.svc.answer(sid, q["question_id"], "deny", selected_option="deny")
    (result,) = h.results(sid)

    assert result["out"]["ok"] is False
    assert status_of(result) == "approval_declined"
    assert "declined" in result["out"]["error"]
    time.sleep(0.3)
    assert h.upstream.count() == 0


def test_write_call_unanswered_times_out_and_is_not_sent(harness):
    h = harness(approval_timeout_s=1.0)
    sid, _sync = h.submit([{"call": "ck_send", "args": {"campaign_id": "c9", "subject": "Hello"}}])

    q = h.pending_approval(sid)
    (result,) = h.results(sid)

    assert result["out"]["ok"] is False
    assert status_of(result) == "approval_timeout"
    # The question is gone once it timed out, so a late yes cannot send it.
    assert h.approvals_for(sid) == []
    with pytest.raises(KeyError):
        h.svc.answer(sid, q["question_id"], "approve", selected_option="approve")
    time.sleep(0.3)
    assert h.upstream.count() == 0


def test_cancel_during_approval_wait_never_sends(harness):
    h = harness()
    sid, _sync = h.submit([{"call": "ck_send", "args": {"campaign_id": "c9", "subject": "Hello"}}])
    q = h.pending_approval(sid)
    socket_path = Path(h.broker.bindings()[sid].socket_path)

    assert h.svc.cancel(sid)["status"] == "cancelled"
    assert sid not in h.broker.bindings()
    assert not socket_path.exists()
    with pytest.raises(KeyError):
        h.svc.answer(sid, q["question_id"], "approve", selected_option="approve")
    time.sleep(0.5)
    assert h.upstream.count() == 0


def test_revoke_mid_run_fails_next_call_closed(harness, monkeypatch):
    h = harness()
    # An operator value on the service env must not stand in for a revoked row.
    monkeypatch.setenv("FABRI_CRED_CK_ACME", ENV_FALLBACK)
    sid, sync = h.submit([
        {"call": "ck_get", "args": {"list_id": "1"}},
        {"mark": "first_done"},
        {"wait": "revoked"},
        {"call": "ck_get", "args": {"list_id": "1"}},
    ])
    wait_file(sync / "first_done")
    assert h.store.set_status("ck", "acme", "revoked")
    touch(sync / "revoked")
    first, second = h.results(sid)

    assert first["out"]["ok"] is True, first["out"].get("error")
    assert second["out"]["ok"] is False
    assert status_of(second) == "credential_unavailable"
    assert second["out"]["error"] == "ck_get: the stored ck credential is revoked; run fabri connect ck"
    assert h.upstream.auth_labels() == ["TOKEN"]
    # The service env value never reached the run either.
    fallback_in_run = ENV_FALLBACK in (sync / "run_env.json").read_text()
    assert not fallback_in_run, "a service env credential reached the run env"


def test_reconnect_mid_run_takes_effect_next_call(harness):
    h = harness()
    sid, sync = h.submit([
        {"call": "ck_get", "args": {"list_id": "1"}},
        {"mark": "first_done"},
        {"wait": "revoked"},
        {"call": "ck_get", "args": {"list_id": "1"}},
        {"mark": "second_done"},
        {"wait": "reconnected"},
        {"call": "ck_get", "args": {"list_id": "1"}},
    ])
    wait_file(sync / "first_done")
    h.store.set_status("ck", "acme", "revoked")
    touch(sync / "revoked")
    wait_file(sync / "second_done")
    # fabri connect ck again: a new token for the same handle.
    h.store.upsert(provider="ck", handle="acme", access_token=NEW_TOKEN)
    touch(sync / "reconnected")
    first, second, third = h.results(sid)

    assert first["out"]["ok"] is True, first["out"].get("error")
    assert status_of(second) == "credential_unavailable"
    assert third["out"]["ok"] is True, third["out"].get("error")
    assert h.upstream.auth_labels() == ["TOKEN", "NEW_TOKEN"]


def test_bash_tool_in_run_cannot_reach_broker(harness):
    h = harness()
    sid, sync = h.submit([
        {"wait": "go"},
        {"call": "bash", "args": {"command": f"'{sys.executable}' '{h.tmp / 'probe.py'}' '{h.tmp}/sync1'"}},
        {"call": "ck_get", "args": {"list_id": "1"}},
    ])
    binding = h.broker.bindings()[sid]
    key = binding.key
    (sync / "sock_path").write_text(binding.socket_path)
    touch(sync / "go")
    bash, read = h.results(sid)

    assert bash["out"]["ok"] is True, bash["out"].get("error")
    assert bash["out"]["result"]["exit_code"] == 0, bash["out"]["result"].get("stderr")
    probe = json.loads(bash["out"]["result"]["stdout"])
    # The tool process got neither broker name nor any other FABRI_CRED_ var.
    assert probe["cred_names"] == []
    tool_env = (sync / "tool_env.json").read_text()
    key_in_tool_env = key in tool_env
    assert not key_in_tool_env, "the broker key reached the bash tool env"
    token_in_tool_env = TOKEN in tool_env
    assert not token_in_tool_env, "the token reached the bash tool env"
    # Knowing the socket path is not enough: every guess is refused, and no
    # request reached the provider from the tool.
    assert len(probe["replies"]) == 2
    for reply in probe["replies"]:
        assert reply is not None and reply["ok"] is False
        assert reply["result"]["status"] == "refused"
        assert reply["error"] == "credential broker refused the capability"
    # The run itself still works after the probe.
    assert read["out"]["ok"] is True, read["out"].get("error")
    assert h.upstream.auth_labels() == ["TOKEN"]


def test_key_from_another_run_is_refused(harness):
    h = harness()
    # Run B holds a binding and waits.
    sid_b, sync_b = h.submit([{"mark": "b_up"}, {"wait": "b_done"}])
    # Run A sends a line with its own key to B's socket.
    sid_a, sync_a = h.submit([{"raw": "other_sock"}, {"call": "ck_get", "args": {"list_id": "1"}}])
    wait_file(sync_b / "b_up")
    bindings = h.broker.bindings()
    b_path = bindings[sid_b].socket_path
    assert b_path != bindings[sid_a].socket_path
    assert bindings[sid_a].key != bindings[sid_b].key
    (sync_a / "other_sock").write_text(b_path)

    raw, own = h.results(sid_a)
    touch(sync_b / "b_done")
    h.results(sid_b)

    # A is not in B's process tree, so B's broker closes without a reply.
    assert raw["raw"] == {"reply": ""}
    # A's own socket still serves A.
    assert own["out"]["ok"] is True, own["out"].get("error")
    assert h.upstream.auth_labels() == ["TOKEN"]


def test_right_key_from_outside_the_run_tree_is_refused(harness):
    h = harness()
    sid, sync = h.submit([{"mark": "up"}, {"wait": "done"}])
    wait_file(sync / "up")
    binding = h.broker.bindings()[sid]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(binding.socket_path)
        line = {"v": 1, "key": binding.key, "op": "http", "source": "ck_get", "tool": "ck_get",
                "path_params": {"list_id": "1"}}
        s.sendall((json.dumps(line) + "\n").encode())
        reply = s.recv(65536)
    finally:
        s.close()
    touch(sync / "done")
    h.results(sid)

    # The test process is the run's parent, not a descendant: closed, no reply.
    assert reply == b""
    assert h.upstream.count() == 0


def test_same_uid_run_is_refused_without_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        monkeypatch.delenv("FABRI_ALLOW_SHARED_UID", raising=False)
        with pytest.raises(ValueError) as excinfo:
            h.submit([{"call": "ck_get", "args": {}}])
        message = str(excinfo.value)
        assert "FABRI_ALLOW_SHARED_UID=1" in message
        assert "installs.db" in message
        assert TOKEN not in message
        assert h.broker.bindings() == {}
        assert h.upstream.count() == 0
    finally:
        h.close()
