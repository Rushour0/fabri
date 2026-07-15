"""Human-in-the-loop over `fabri serve`: the `ask_user` tool routed to the
service's per-run Unix socket, surfaced to a client as an `ask_user` trace event
(over the existing SSE stream), and answered via `POST /runs/<id>/answer`.

Fully offline: no LLM. The agent subprocess is stubbed by a fake script that
speaks the same ask_user socket protocol the real tool does
(tools/examples/ask_user.py), blocks for the reply, and echoes it back in its
final text -- so a test can assert the answer actually reached the run.
"""
import http.client
import json
import shutil
import socket
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from fabri.service.ask_user_listener import AskUserListener
from fabri.service.service import FabriService


@pytest.fixture
def short_socket_dir():
    """AF_UNIX paths are capped (~104 bytes on macOS); pytest's tmp_path is too
    deep. Use a shallow temp dir so the socket path stays well under the cap."""
    d = tempfile.mkdtemp(prefix="fabri-ask-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# --- fake agent that asks a question -----------------------------------------

# Connects to FABRI_ASK_USER_SOCKET, sends one question, blocks for the reply,
# and writes a trace + envelope whose final text carries the answer back.
_ASKING_AGENT = """
import json, os, socket, uuid
from pathlib import Path

home = Path(os.environ["FABRI_HOME"])
sid = os.environ["FABRI_SESSION_ID"]
trace = home / ".fabri" / "traces" / (sid + ".jsonl")
trace.parent.mkdir(parents=True, exist_ok=True)
with trace.open("a") as f:
    f.write(json.dumps({"type": "start", "task": "t"}) + "\\n")

sock_path = os.environ["FABRI_ASK_USER_SOCKET"]
qid = str(uuid.uuid4())
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
conn = s.makefile("rwb")
conn.write((json.dumps({
    "kind": "ask_user", "question_id": qid,
    "question": "Ship it?", "options": ["yes", "no"], "default": "no",
}) + "\\n").encode())
conn.flush()
reply = json.loads(conn.readline().decode())
answer = reply.get("answer", "")
s.close()

with trace.open("a") as f:
    f.write(json.dumps({"type": "final", "text": "answered: " + answer,
                        "outcome": "success"}) + "\\n")
    f.write(json.dumps({"type": "usage", "total_cost_usd": 0.001}) + "\\n")

print(json.dumps({
    "session_id": sid, "success": True,
    "final_text": "answered: " + answer, "outcome": "success",
    "usage": {"total_cost_usd": 0.001},
}))
"""


@pytest.fixture
def asking_agent(tmp_path: Path) -> Path:
    p = tmp_path / "asking_agent.py"
    p.write_text(textwrap.dedent(_ASKING_AGENT))
    return p


def _builder_for(script: Path):
    def _build(task, config_path, session_id, fabri_home):
        return [sys.executable, str(script)]

    return _build


# --- 1. AskUserListener in isolation -----------------------------------------

def test_listener_round_trip(short_socket_dir: Path):
    """A raw client connects, asks, and receives the answer we deliver."""
    tmp_path = short_socket_dir
    seen: list[dict] = []
    sock_path = str(tmp_path / "ask.sock")
    listener = AskUserListener(sock_path, on_question=seen.append)
    listener.start()
    try:
        qid = str(uuid.uuid4())

        def ask() -> dict:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            f = s.makefile("rwb")
            f.write((json.dumps({"question_id": qid, "question": "Q?"}) + "\n").encode())
            f.flush()
            reply = json.loads(f.readline().decode())
            s.close()
            return reply

        result: dict = {}
        t = threading.Thread(target=lambda: result.update(ask()), daemon=True)
        t.start()

        # The question surfaces via on_question; then we answer it.
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen and seen[0]["question_id"] == qid
        assert seen[0]["question"] == "Q?"

        assert listener.answer(qid, "the answer") is True
        t.join(timeout=5)
        assert result["answer"] == "the answer"
        assert result["question_id"] == qid
    finally:
        listener.close()


def test_listener_answer_unknown_question_returns_false(short_socket_dir: Path):
    tmp_path = short_socket_dir
    listener = AskUserListener(str(tmp_path / "ask.sock"), on_question=lambda q: None)
    listener.start()
    try:
        assert listener.answer("nope", "x") is False
    finally:
        listener.close()


def test_listener_close_unlinks_socket(short_socket_dir: Path):
    tmp_path = short_socket_dir
    sock_path = tmp_path / "ask.sock"
    listener = AskUserListener(str(sock_path), on_question=lambda q: None)
    listener.start()
    assert sock_path.exists()
    listener.close()
    assert not sock_path.exists()


# --- 2. Service-level: submit -> question -> answer -> completes --------------

def _stream_until_ask(svc: FabriService, session_id: str, holder: dict) -> None:
    """Collect the run's events; record the first ask_user question_id + latch."""
    for ev in svc.stream(session_id, timeout=30):
        holder.setdefault("events", []).append(ev)
        if ev.get("type") == "ask_user" and "qid" not in holder:
            holder["qid"] = ev["question_id"]
            holder["asked"].set()


def test_service_ask_user_round_trip(tmp_path: Path, asking_agent: Path):
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(asking_agent),
    )
    try:
        session_id = svc.submit("go")
        holder: dict = {"asked": threading.Event()}
        streamer = threading.Thread(
            target=_stream_until_ask, args=(svc, session_id, holder), daemon=True
        )
        streamer.start()

        assert holder["asked"].wait(timeout=15), "no ask_user event surfaced"
        # The question reached the client over the existing event stream, with
        # its question_id and the tool's options intact.
        ask_ev = next(e for e in holder["events"] if e["type"] == "ask_user")
        assert ask_ev["options"] == ["yes", "no"]
        assert ask_ev["default"] == "no"

        svc.answer(session_id, holder["qid"], "yes", selected_option="yes")

        result = svc.result(session_id, timeout=30)
        assert result["success"] is True
        assert result["final_text"] == "answered: yes"
        streamer.join(timeout=5)
    finally:
        svc.close()


def test_service_answer_unknown_session_raises(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    with pytest.raises(KeyError):
        svc.answer("nope", "q", "a")


# --- 3. HTTP: POST /runs/<id>/answer unblocks the run ------------------------

def _read_sse_until(resp, holder: dict) -> None:
    """Parse an SSE response line-by-line; capture the ask_user question_id and
    the terminal result frame as they arrive."""
    event_name = None
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode().rstrip("\n")
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[len("data:"):].strip())
            if event_name == "result":
                holder["result"] = payload
            elif payload.get("type") == "ask_user" and "qid" not in holder:
                holder["qid"] = payload["question_id"]
                holder["asked"].set()
            event_name = None


def test_http_answer_round_trip(tmp_path: Path, asking_agent: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(asking_agent),
    )
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # submit
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("POST", "/runs", body=json.dumps({"task": "go"}),
                     headers={"Content-Type": "application/json"})
        session_id = json.loads(conn.getresponse().read().decode())["session_id"]
        conn.close()

        # stream events on a background connection
        stream_conn = http.client.HTTPConnection(host, port, timeout=30)
        stream_conn.request("GET", f"/runs/{session_id}/events")
        resp = stream_conn.getresponse()
        holder: dict = {"asked": threading.Event()}
        streamer = threading.Thread(target=_read_sse_until, args=(resp, holder), daemon=True)
        streamer.start()

        # once the question surfaces, answer it over HTTP
        assert holder["asked"].wait(timeout=15), "no ask_user frame over SSE"
        ans = http.client.HTTPConnection(host, port, timeout=30)
        ans.request("POST", f"/runs/{session_id}/answer",
                    body=json.dumps({"question_id": holder["qid"], "answer": "yes",
                                     "selected_option": "yes"}),
                    headers={"Content-Type": "application/json"})
        ans_resp = ans.getresponse()
        assert ans_resp.status == 200
        assert json.loads(ans_resp.read().decode())["status"] == "answered"
        ans.close()

        streamer.join(timeout=20)
        assert holder.get("result") is not None
        assert holder["result"]["final_text"] == "answered: yes"
    finally:
        server.shutdown()
        svc.close()


def test_http_answer_unknown_session_404(tmp_path: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(home_root=tmp_path / "runs")
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("POST", "/runs/nope/answer",
                     body=json.dumps({"question_id": "q", "answer": "a"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 404
    finally:
        server.shutdown()
        svc.close()
