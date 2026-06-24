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
