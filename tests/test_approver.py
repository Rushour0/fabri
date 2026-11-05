"""Tests for fabri.tools.approver: the terminal and socket approvers and the
rule that picks one."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from fabri.tools.approver import (
    SOCKET_ENV,
    TIMEOUT_ENV,
    SocketApprover,
    TtyApprover,
    default_approver,
    describe_call,
)
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry
from fabri.tools.result import tool_ok


class FakeTty(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _manifest(approval: str = "always_approve", title: str | None = "Send a campaign") -> ToolManifest:
    return ToolManifest(
        name="send_campaign",
        description="send it",
        command=[],
        input_schema={"type": "object", "properties": {}},
        output_schema={},
        approval=approval,
        title=title,
    )


@pytest.fixture
def short_socket_dir():
    """AF_UNIX paths are capped at 104 bytes on macOS, so keep the socket
    under /tmp rather than pytest's deep tmp_path."""
    d = tempfile.mkdtemp(prefix="fabri-appr-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _serve_once(socket_path: str, reply_for, seen: list) -> threading.Thread:
    """Accept one connection, record the request, send reply_for(payload)
    unless it returns None (then hold the connection open until the client
    gives up)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    srv.listen(1)
    srv.settimeout(5.0)

    def run():
        try:
            conn, _ = srv.accept()
            with conn:
                f = conn.makefile("rb")
                payload = json.loads(f.readline().decode("utf-8"))
                seen.append(payload)
                reply = reply_for(payload)
                if reply is None:
                    conn.settimeout(5.0)
                    try:
                        conn.recv(1)
                    except OSError:
                        pass
                    return
                conn.sendall(reply if isinstance(reply, bytes) else (json.dumps(reply) + "\n").encode())
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_default_approver_prefers_socket_env(short_socket_dir):
    path = str(short_socket_dir / "a.sock")
    approver = default_approver(
        env={SOCKET_ENV: path, TIMEOUT_ENV: "7"}, stdin=FakeTty("y\n"), stderr=FakeTty()
    )
    assert isinstance(approver, SocketApprover)
    assert approver.socket_path == path
    assert approver.timeout_s == 7.0


def test_default_approver_tty_yes_and_no():
    for answer, expected in (("y\n", True), ("yes\n", True), ("YES\n", True),
                             ("n\n", False), ("\n", False), ("sure\n", False), ("approve\n", False)):
        err = FakeTty()
        approver = default_approver(env={}, stdin=FakeTty(answer), stderr=err)
        assert isinstance(approver, TtyApprover)
        assert approver("send_campaign", {"id": 1}, _manifest()) is expected, answer
        shown = err.getvalue()
        assert "send_campaign needs your approval (class always_approve)" in shown
        assert "title: Send a campaign" in shown
        assert shown.endswith("approve? [y/N] ")


def test_default_approver_none_without_tty_or_socket():
    assert default_approver(env={}, stdin=FakeTty(tty=False), stderr=FakeTty()) is None
    assert default_approver(env={}, stdin=FakeTty(), stderr=FakeTty(tty=False)) is None
    # No env var grants a standing yes.
    env = {"FABRI_APPROVE": "1", "FABRI_AUTO_APPROVE": "yes", "YES": "1"}
    assert default_approver(env=env, stdin=FakeTty(tty=False), stderr=FakeTty(tty=False)) is None


def test_tty_denies_on_eof():
    err = FakeTty()
    approver = TtyApprover(stdin=FakeTty(""), stderr=err)
    assert approver("send_campaign", {}, _manifest()) is False
    assert "denied" in err.getvalue()


def test_tty_denies_when_stream_stops_being_a_terminal():
    stdin = FakeTty("y\n")
    approver = TtyApprover(stdin=stdin, stderr=FakeTty())
    stdin._tty = False
    assert approver("send_campaign", {}, _manifest()) is False
    assert stdin.read() == "y\n"  # nothing was consumed


def test_socket_approve_by_option_and_by_answer(short_socket_dir):
    cases = [
        ({"answer": "", "selected_option": "approve"}, True),
        ({"answer": "Yes "}, True),
        ({"answer": "approve"}, True),
        ({"answer": "y"}, True),
        ({"answer": "deny", "selected_option": "deny"}, False),
        ({"answer": "maybe"}, False),
        ({"answer": "approve", "selected_option": "deny"}, False),
        ({"answer": "yes", "selected_option": None}, True),
        ({"answer": "no way"}, False),
    ]
    for i, (reply, expected) in enumerate(cases):
        path = str(short_socket_dir / f"s{i}.sock")
        seen: list = []
        t = _serve_once(path, lambda p, r=reply: {"question_id": p["question_id"], **r}, seen)
        assert SocketApprover(path, timeout_s=5)("send_campaign", {"id": 9}, _manifest()) is expected, reply
        t.join(5)
        payload = seen[0]
        assert payload["kind"] == "approval"
        assert payload["options"] == ["approve", "deny"]
        assert payload["default"] == "deny"
        assert payload["question"] == describe_call("send_campaign", {"id": 9}, _manifest())
        assert len(payload["question_id"]) == 32


def test_socket_question_id_must_match(short_socket_dir, capsys):
    cases = [
        ({"question_id": "other", "selected_option": "approve"}, False),
        ({"question_id": "other", "answer": "yes"}, False),
        ({"selected_option": "approve"}, True),  # no id in the reply is accepted
    ]
    for i, (reply, expected) in enumerate(cases):
        path = str(short_socket_dir / f"q{i}.sock")
        t = _serve_once(path, lambda p, r=reply: r, [])
        assert SocketApprover(path, timeout_s=5)("send_campaign", {}, _manifest()) is expected, reply
        t.join(5)
    assert capsys.readouterr().err.count("different question_id") == 2


def test_socket_timeout_denies(short_socket_dir, capsys):
    path = str(short_socket_dir / "t.sock")
    t = _serve_once(path, lambda p: None, [])
    assert SocketApprover(path, timeout_s=0.3)("send_campaign", {}, _manifest()) is False
    t.join(6)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "no answer for send_campaign" in err


def test_socket_unreachable_raises(short_socket_dir):
    with pytest.raises(RuntimeError, match="unreachable"):
        SocketApprover(str(short_socket_dir / "missing.sock"), timeout_s=1)("x", {}, _manifest())

    path = str(short_socket_dir / "bad.sock")
    t = _serve_once(path, lambda p: b"not json\n", [])
    with pytest.raises(RuntimeError, match="not JSON"):
        SocketApprover(path, timeout_s=5)("x", {}, _manifest())
    t.join(5)

    path = str(short_socket_dir / "closed.sock")
    t = _serve_once(path, lambda p: b"", [])
    with pytest.raises(RuntimeError, match="closed"):
        SocketApprover(path, timeout_s=5)("x", {}, _manifest())
    t.join(5)


def test_registry_integration_declined(tmp_path):
    calls: list = []

    def handler(args):
        calls.append(args)
        return tool_ok({"sent": True})

    reg = ToolRegistry(tmp_path, approver=TtyApprover(stdin=FakeTty("n\n"), stderr=FakeTty()))
    reg.register_callable(_manifest(), handler)
    result = reg.invoke("send_campaign", {"id": 1})
    assert result["ok"] is False
    assert "a human declined this call" in result["error"]
    assert calls == []

    reg.approver = TtyApprover(stdin=FakeTty("yes\n"), stderr=FakeTty())
    assert reg.invoke("send_campaign", {"id": 2})["ok"] is True
    assert calls == [{"id": 2}]

    # A broken socket shows up as a failed approval check and the call is not made.
    reg.approver = SocketApprover("/tmp/fabri-no-such-dir/x.sock", timeout_s=1)
    result = reg.invoke("send_campaign", {"id": 3})
    assert result["ok"] is False
    assert "approval check failed" in result["error"]
    assert calls == [{"id": 2}]


def test_describe_call_caps_args():
    args = {"body": "x" * 5000}
    text = describe_call("send_campaign", args, _manifest(title=None), cap=100)
    full = json.dumps(args, indent=2, sort_keys=True)
    assert text.startswith("send_campaign needs your approval (class always_approve)\nargs:\n")
    assert "title:" not in text
    assert text.endswith(f"... ({len(full) - 100} more chars)")
    assert full[:100] in text

    short = describe_call("t", {"b": 2, "a": 1}, _manifest(approval="write"))
    assert short.splitlines()[0] == "t needs your approval (class write)"
    assert short.index('"a"') < short.index('"b"')
