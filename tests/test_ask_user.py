"""A1 -- ask_user. Three angles:
1. Manifest is registered as a builtin.
2. Socket transport: spin up a one-shot Unix-socket server in a thread, the
   tool sends one question and gets one reply.
3. Stdin fallback: no FABRI_ASK_USER_SOCKET set, tool reads the answer from
   stdin (default value applies when stdin is empty).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
)
TOOL_SCRIPT = EXAMPLES_DIR / "ask_user.py"


def _serve_one(socket_path: str, reply_for: callable, stop: threading.Event):
    """Accept one connection, read one JSON line, write `reply_for(payload)`
    as JSON + newline, close. Single-shot; the tool only opens one socket per
    invocation."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.settimeout(5.0)
    srv.bind(socket_path)
    srv.listen(1)
    try:
        conn, _ = srv.accept()
        with conn:
            f = conn.makefile("rwb")
            line = f.readline()
            payload = json.loads(line.decode("utf-8"))
            reply = reply_for(payload)
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except Exception:
        pass
    finally:
        srv.close()
        stop.set()


@pytest.fixture
def short_socket_dir():
    """AF_UNIX paths are capped at 104 bytes on macOS; pytest's tmp_path is
    too deep. Use /tmp directly so the socket path stays well under the cap."""
    d = tempfile.mkdtemp(prefix="fabri-ask-")
    yield Path(d)
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_ask_user_registered_as_builtin():
    from fabri.tools.registry import ToolRegistry

    reg = ToolRegistry(EXAMPLES_DIR)
    assert "ask_user" in reg.tools


def test_socket_transport_round_trip(short_socket_dir):
    tmp_path = short_socket_dir
    socket_path = str(tmp_path / "ask.sock")
    stop = threading.Event()

    def reply_for(payload):
        # Echo back the same question_id with a canned answer.
        assert payload["kind"] == "ask_user"
        assert payload["question"] == "village size?"
        assert payload["options"] == ["small", "medium", "large"]
        return {"question_id": payload["question_id"], "answer": "medium", "selected_option": "medium"}

    t = threading.Thread(target=_serve_one, args=(socket_path, reply_for, stop), daemon=True)
    t.start()

    env = os.environ.copy()
    env["FABRI_ASK_USER_SOCKET"] = socket_path
    proc = subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=json.dumps({
            "question": "village size?",
            "options": ["small", "medium", "large"],
        }),
        capture_output=True, text=True, env=env, timeout=10,
    )
    t.join(timeout=5)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["answer"] == "medium"
    assert payload["selected_option"] == "medium"


def test_question_id_mismatch_is_an_error(short_socket_dir):
    tmp_path = short_socket_dir
    socket_path = str(tmp_path / "ask.sock")
    stop = threading.Event()

    def reply_for(payload):
        # Deliberately wrong question_id -- a misrouted reply must not be
        # silently accepted (concurrent sub-agents could race here).
        return {"question_id": "WRONG", "answer": "x"}

    t = threading.Thread(target=_serve_one, args=(socket_path, reply_for, stop), daemon=True)
    t.start()
    env = os.environ.copy()
    env["FABRI_ASK_USER_SOCKET"] = socket_path
    proc = subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=json.dumps({"question": "?"}),
        capture_output=True, text=True, env=env, timeout=10,
    )
    t.join(timeout=5)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "mismatch" in payload["error"]


def test_stdin_fallback_uses_default(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "FABRI_ASK_USER_SOCKET"}
    proc = subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=json.dumps({"question": "yes?", "default": "yes"}) + "\n",
        capture_output=True, text=True, env=env, timeout=10,
    )
    # Tool reads its question JSON from stdin first; the trailing blank line
    # is what readline() picks up as the (empty) answer, so the default fires.
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["answer"] == "yes"


def test_missing_question_returns_error():
    proc = subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=json.dumps({"options": ["a"]}),
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "missing required field" in payload["error"]


def test_socket_empty_reply_falls_back_to_default(short_socket_dir):
    """An empty answer over the socket must apply the question's `default`
    (parity with the stdin transport, which already did)."""
    socket_path = str(short_socket_dir / "ask.sock")
    stop = threading.Event()

    def reply_for(payload):
        return {"question_id": payload["question_id"], "answer": ""}

    t = threading.Thread(target=_serve_one, args=(socket_path, reply_for, stop), daemon=True)
    t.start()

    env = os.environ.copy()
    env["FABRI_ASK_USER_SOCKET"] = socket_path
    proc = subprocess.run(
        [sys.executable, str(TOOL_SCRIPT)],
        input=json.dumps({"question": "proceed?", "default": "yes"}),
        capture_output=True, text=True, env=env, timeout=10,
    )
    t.join(timeout=5)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["answer"] == "yes"


# -- AskUserListener: questions asked from inside the service process ---------

def _listener(short_socket_dir, events):
    from fabri.service.ask_user_listener import AskUserListener

    return AskUserListener(str(short_socket_dir / "q.sock"), events.append)


def test_ask_local_fires_on_question_and_is_listed(short_socket_dir):
    events: list[dict] = []
    lst = _listener(short_socket_dir, events)
    p = lst.ask_local(
        question_id="q1", question="send it?", options=["approve", "deny"],
        default="deny", kind="approval",
    )
    assert p.conn is None
    assert events == [{
        "question_id": "q1", "kind": "approval", "question": "send it?",
        "options": ["approve", "deny"], "default": "deny",
    }]
    listed = lst.pending_questions()
    assert [q["question_id"] for q in listed] == ["q1"]
    assert listed[0]["kind"] == "approval"
    lst.forget("q1")
    assert lst.pending_questions() == []


def test_ask_local_answered_through_answer(short_socket_dir):
    lst = _listener(short_socket_dir, [])
    p = lst.ask_local(question_id="q1", question="ok?", options=None, default=None)
    assert not p.event.is_set()
    assert lst.answer("q1", "approve", "approve") is True
    assert p.event.is_set()
    assert p.reply == {
        "question_id": "q1", "answer": "approve", "selected_option": "approve",
    }
    lst.forget("q1")
    lst.forget("q1")  # second forget is a no-op
    assert lst.answer("q1", "approve") is False


def test_ask_local_close_releases_without_reply(short_socket_dir):
    lst = _listener(short_socket_dir, [])
    p = lst.ask_local(question_id="q1", question="ok?", options=None, default=None)
    lst.close()
    assert p.event.is_set()
    assert p.reply is None
    lst.forget("q1")  # after close, still a no-op


def test_ask_local_duplicate_id_rejected(short_socket_dir):
    lst = _listener(short_socket_dir, [])
    first = lst.ask_local(question_id="q1", question="a", options=None, default=None)
    with pytest.raises(ValueError):
        lst.ask_local(question_id="q1", question="b", options=None, default=None)
    assert lst._pending["q1"] is first


def test_ask_local_survives_a_failing_on_question_hook(short_socket_dir):
    from fabri.service.ask_user_listener import AskUserListener

    def boom(_event):
        raise RuntimeError("hook down")

    lst = AskUserListener(str(short_socket_dir / "q.sock"), boom)
    p = lst.ask_local(question_id="q1", question="ok?", options=None, default=None)
    assert [q["question_id"] for q in lst.pending_questions()] == ["q1"]
    assert lst.answer("q1", "yes") is True
    assert p.reply["answer"] == "yes"


def test_socket_questions_unchanged_next_to_local_ones(short_socket_dir):
    events: list[dict] = []
    lst = _listener(short_socket_dir, events)
    lst.start()
    try:
        local = lst.ask_local(
            question_id="local1", question="local?", options=None, default=None,
            kind="approval",
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(lst.socket_path)
        client.sendall((json.dumps({
            "question_id": "sock1", "question": "socket?", "options": ["a"],
            "default": "a",
        }) + "\n").encode())
        deadline = time.time() + 5.0
        while len(lst.pending_questions()) < 2 and time.time() < deadline:
            time.sleep(0.02)
        ids = {q["question_id"] for q in lst.pending_questions()}
        assert ids == {"local1", "sock1"}
        assert {e["question_id"] for e in events} == {"local1", "sock1"}

        assert lst.answer("sock1", "a", "a") is True
        data = b""
        while b"\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        assert json.loads(data.split(b"\n")[0]) == {
            "question_id": "sock1", "answer": "a", "selected_option": "a",
        }
        # The local one is untouched by the socket answer.
        assert not local.event.is_set()
        assert local.reply is None
        client.close()
    finally:
        lst.close()
