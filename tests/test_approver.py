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
        ({"selected_option": "approve"}, False),  # a reply must carry the id
        ({"question_id": None, "answer": "yes"}, False),
    ]
    for i, (reply, expected) in enumerate(cases):
        path = str(short_socket_dir / f"q{i}.sock")
        t = _serve_once(path, lambda p, r=reply: r, [])
        assert SocketApprover(path, timeout_s=5)("send_campaign", {}, _manifest()) is expected, reply
        t.join(5)
    assert capsys.readouterr().err.count("did not carry this question's id") == 4


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


def test_describe_call_refuses_args_over_cap():
    # Over the cap the card is never cut short: it raises, so the call that
    # would run the unseen tail is not made.
    from fabri.tools.approver import ArgsTooLong

    args = {"body": "x" * 5000}
    with pytest.raises(ArgsTooLong, match="arguments too long to show in full"):
        describe_call("send_campaign", args, _manifest(title=None), cap=100)
    text = describe_call("send_campaign", args, _manifest(title=None))
    full = json.dumps(args, indent=2, sort_keys=True)
    assert text.startswith("send_campaign needs your approval (class always_approve)\nargs:\n")
    assert "title:" not in text
    assert text.endswith(full)

    short = describe_call("t", {"b": 2, "a": 1}, _manifest(approval="write"))
    assert short.splitlines()[0] == "t needs your approval (class write)"
    assert short.index('"a"') < short.index('"b"')


def test_cmd_run_uses_default_approver(monkeypatch):
    """fabri run builds its registry with default_approver(), so a terminal
    run asks y/N and a run under fabri serve asks through the socket."""
    import argparse

    from fabri import cli

    sentinel = TtyApprover(stdin=FakeTty(), stderr=FakeTty())
    seen: dict = {}

    class Stop(Exception):
        pass

    def fake_build_tools(tools_cfg, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(cli, "load_config", lambda _p: {
        "llm": {"api_key_env": "FAKE_KEY"},
        "memory": {"qdrant_url": "x", "collection": "c"},
        "tools": {"decompose": {"max_subquestions": 1}},
        "agent": {"max_steps": 1},
    })
    monkeypatch.setattr(cli, "_require_role_api_keys", lambda _c: None)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_open_store", lambda _c: object())
    monkeypatch.setattr(cli, "default_approver", lambda: sentinel)
    monkeypatch.setattr(cli, "build_tools", fake_build_tools)
    monkeypatch.setattr(cli, "run_agent", lambda *a, **k: pytest.fail("run_agent must not be reached"))

    args = argparse.Namespace(config=None, task="t", session_id="sid", verbose=False, ask_user_socket=None)
    with pytest.raises(Stop):
        cli.cmd_run(args)
    assert seen == {"approver": sentinel}


def test_describe_call_redacts_secret_keys_at_any_depth_but_keeps_message_fields():
    from fabri.tools.approver import describe_call

    args = {
        "to": "ann@example.test",
        "body": "Hi Ann, the launch is Friday.",
        "Password": "hunter2",
        "auth": {"api_key": "sk-live-1", "Authorization": "Bearer abc", "note": "keep me"},
        "headers": [{"X-Cookie": "sid=9"}, {"name": "ok"}],
        "client_secret_id": "s3",
        "db_credentials": {"user": "u"},
        "apikey": "k",
        "access_token": "t",
        "passwd": "p",
    }
    text = describe_call("send_mail", args, None)
    for leaked in ("hunter2", "sk-live-1", "Bearer abc", "sid=9", "s3", "sk-", "\"k\"", "\"t\"", "\"p\""):
        assert leaked not in text
    assert text.count("[redacted]") == 9
    for kept in ("ann@example.test", "Hi Ann, the launch is Friday.", "keep me", "\"ok\""):
        assert kept in text
    assert args["Password"] == "hunter2" and args["auth"]["api_key"] == "sk-live-1"


@pytest.mark.parametrize("args, secret", [
    ({"headers": [{"name": "Authorization", "value": "Bearer x"}]}, "Bearer x"),
    ({"headers": [{"name": "X-Api-Key", "value": "plainvalue1"}]}, "plainvalue1"),
    ({"auth": {"type": "bearer", "key": "opaque-123"}}, "opaque-123"),
    ({"headers": [{"name": "X-Api-Key", "key": "opaque-123"}]}, "opaque-123"),
    ({"h": "Authorization: Bearer opaque-123"}, "opaque-123"),
    ({"h": "x-api-key: opaque-123"}, "opaque-123"),
    ({"h": "COOKIE: sid=opaque-123; a=b"}, "opaque-123"),
    ({"auth": {"type": "bearer", "value": "opaque-123"}}, "opaque-123"),
    ({"fields": [{"field": "client_secret", "content": "shh-42"}]}, "shh-42"),
    ({"h": "Bearer abc.def"}, "Bearer abc.def"),
    ({"h": "Basic dXNlcjpwYXNz"}, "dXNlcjpwYXNz"),
    ({"v": "sk-proj-AbCdEf1234567890"}, "sk-proj-AbCdEf1234567890"),
    ({"v": "sk_live_51HxYzAbCdEf"}, "sk_live_51HxYzAbCdEf"),
    ({"v": "pk_live_51HxYzAbCdEf"}, "pk_live_51HxYzAbCdEf"),
    ({"v": "ghp_" + "a1B2" * 9}, "ghp_" + "a1B2" * 9),
    ({"v": "gho_" + "a1B2" * 9}, "gho_" + "a1B2" * 9),
    ({"v": "github_pat_11ABCDEFG0123456789_xyz"}, "github_pat_11ABCDEFG0123456789_xyz"),
    ({"v": "xoxb-1234-5678-abcdEFGH"}, "xoxb-1234-5678-abcdEFGH"),
    ({"v": "xoxp-1234-5678-abcdEFGH"}, "xoxp-1234-5678-abcdEFGH"),
    ({"v": "ck_pat_abc123DEF456"}, "ck_pat_abc123DEF456"),
    ({"v": "AKIAIOSFODNN7EXAMPLE"}, "AKIAIOSFODNN7EXAMPLE"),
    ({"v": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"}, "eyJhbGciOiJIUzI1NiJ9"),
])
def test_redact_args_catches_labelled_values_and_token_shapes(args, secret):
    from fabri.tools.approver import describe_call

    text = describe_call("http_call", args, None)
    assert secret not in text
    assert "[redacted]" in text


def test_redact_args_cuts_a_token_out_of_a_body_and_keeps_the_rest():
    from fabri.tools.approver import redact_args

    args = {
        "to": ["ann@example.test", "bob@example.test"],
        "subject": "Basic question about invoices",
        "body": "Hi Ann, please rotate sk-proj-AbCdEf1234567890 before Friday. Bearer bonds are fine.",
        "headers": [{"name": "Content-Type", "value": "application/json"}],
        "note": "Basic plan",
    }
    out = redact_args(args)
    assert out["to"] == ["ann@example.test", "bob@example.test"]
    assert out["subject"] == "Basic question about invoices"
    assert out["note"] == "Basic plan"
    assert out["body"] == "Hi Ann, please rotate [redacted] before Friday. Bearer bonds are fine."
    assert out["headers"] == [{"name": "Content-Type", "value": "application/json"}]
    # The labels stay so the person can see what was hidden.
    assert redact_args({"name": "Authorization", "value": "Bearer x"}) == {
        "name": "Authorization", "value": "[redacted]"}


def test_header_strings_keep_the_name_and_other_fields_stay_visible():
    from fabri.tools.approver import redact_args

    out = redact_args({
        "to": "ann@example.test",
        "subject": "Quarterly numbers",
        "body": "Hi Ann, see attached.",
        "h": "Authorization: Bearer opaque-123",
    })
    assert out["h"] == "Authorization: [redacted]"
    assert out["to"] == "ann@example.test"
    assert out["subject"] == "Quarterly numbers"
    assert out["body"] == "Hi Ann, see attached."


def test_key_value_header_pair_is_redacted_but_plain_key_is_not():
    from fabri.tools.approver import redact_args
    out = redact_args({"headers": [{"key": "X-Api-Key", "value": "opaque-123"}], "key": "list-7"})
    assert "opaque-123" not in str(out)
    assert out["key"] == "list-7"
    assert out["headers"][0]["key"] == "X-Api-Key"
