"""Unit tests for the new built-in tools (bash, python_exec, grep, fetch_url),
each driven through ToolRegistry so the runner's normalization is also
covered. fetch_url tests use an in-process http.server so they don't depend
on internet access."""
import http.server
import os
import socketserver
import threading
import time
from pathlib import Path

import pytest

from agent_memory.tools.registry import ToolRegistry

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_memory" / "tools" / "examples"


@pytest.fixture
def reg(tmp_path):
    os.environ["AGENT_SANDBOX_ROOT"] = str(tmp_path)
    return ToolRegistry(EXAMPLES_DIR)


# ---------- bash ----------

def test_bash_echo_round_trip(reg):
    r = reg.invoke("bash", {"command": "echo hello"})
    assert r["ok"] is True
    assert r["result"]["exit_code"] == 0
    assert "hello" in r["result"]["stdout"]


def test_bash_nonzero_exit_is_failure(reg):
    r = reg.invoke("bash", {"command": "exit 7"})
    assert r["ok"] is False
    assert r["result"]["exit_code"] == 7


def test_bash_runs_in_sandbox_cwd(reg, tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    r = reg.invoke("bash", {"command": "ls"})
    assert r["ok"] is True
    assert "marker.txt" in r["result"]["stdout"]


def test_bash_timeout_is_distinct_failure(reg):
    r = reg.invoke("bash", {"command": "sleep 5", "timeout": 1})
    assert r["ok"] is False
    assert "timeout" in r["result"]["error"]


def test_bash_truncates_huge_output(reg):
    # 200KB of stdout -- well above the 50KB cap
    r = reg.invoke("bash", {"command": "python3 -c 'print(\"x\"*200000)'"})
    assert r["ok"] is True
    assert r["result"]["stdout_truncated"] is True
    assert len(r["result"]["stdout"].encode()) <= 50_000


# ---------- python_exec ----------

def test_python_exec_round_trip(reg):
    r = reg.invoke("python_exec", {"code": "print(2 + 2)"})
    assert r["ok"] is True
    assert r["result"]["stdout"].strip() == "4"


def test_python_exec_failure_propagates(reg):
    r = reg.invoke("python_exec", {"code": "raise RuntimeError('boom')"})
    assert r["ok"] is False
    assert "RuntimeError" in r["result"]["stderr"]


def test_python_exec_runs_in_sandbox_cwd(reg, tmp_path):
    (tmp_path / "data.txt").write_text("payload")
    r = reg.invoke("python_exec", {"code": "print(open('data.txt').read())"})
    assert r["ok"] is True
    assert r["result"]["stdout"].strip() == "payload"


def test_python_exec_timeout(reg):
    r = reg.invoke("python_exec", {"code": "import time; time.sleep(5)", "timeout": 1})
    assert r["ok"] is False
    assert "timeout" in r["result"]["error"]


# ---------- grep ----------

def test_grep_finds_matches_with_line_numbers(reg, tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\ndef bar():\n    pass\n")
    r = reg.invoke("grep", {"pattern": r"^def "})
    assert r["ok"] is True
    matches = r["result"]["matches"]
    assert {(m["path"], m["line"]) for m in matches} == {("a.py", 1), ("a.py", 3)}


def test_grep_no_matches_is_success_empty_list(reg, tmp_path):
    (tmp_path / "a.txt").write_text("nothing here")
    r = reg.invoke("grep", {"pattern": "xyzzy"})
    assert r["ok"] is True
    assert r["result"]["matches"] == []


def test_grep_invalid_regex_is_failure(reg, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    r = reg.invoke("grep", {"pattern": "[unclosed"})
    assert r["ok"] is False
    assert "invalid regex" in r["result"]["error"]


def test_grep_glob_filters_files(reg, tmp_path):
    (tmp_path / "a.py").write_text("match\n")
    (tmp_path / "b.txt").write_text("match\n")
    r = reg.invoke("grep", {"pattern": "match", "glob": "*.py"})
    assert r["ok"] is True
    paths = {m["path"] for m in r["result"]["matches"]}
    assert paths == {"a.py"}


def test_grep_path_escape_rejected(reg):
    r = reg.invoke("grep", {"pattern": "x", "path": "../.."})
    assert r["ok"] is False
    assert "escapes sandbox root" in r["result"]["error"]


def test_grep_on_single_file(reg, tmp_path):
    (tmp_path / "f.txt").write_text("hit\nmiss\nhit\n")
    r = reg.invoke("grep", {"pattern": "hit", "path": "f.txt"})
    assert r["ok"] is True
    assert len(r["result"]["matches"]) == 2


# ---------- fetch_url ----------

@pytest.fixture
def http_server():
    """Tiny in-process server so fetch_url tests don't need internet."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/ok":
                self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(b"hello body")
            elif self.path == "/big":
                self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(b"x" * 300_000)
            elif self.path == "/notfound":
                self.send_response(404); self.end_headers(); self.wfile.write(b"nope")
            else:
                self.send_response(500); self.end_headers()
        def log_message(self, *a, **k): pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.shutdown()


def test_fetch_url_returns_body(reg, http_server):
    r = reg.invoke("fetch_url", {"url": f"{http_server}/ok"})
    assert r["ok"] is True
    assert r["result"]["status"] == 200
    assert r["result"]["body"] == "hello body"
    assert r["result"]["truncated"] is False


def test_fetch_url_truncates_over_cap(reg, http_server):
    r = reg.invoke("fetch_url", {"url": f"{http_server}/big"})
    assert r["ok"] is True
    assert r["result"]["truncated"] is True
    assert len(r["result"]["body"].encode()) <= 200_000


def test_fetch_url_http_error_is_failure(reg, http_server):
    r = reg.invoke("fetch_url", {"url": f"{http_server}/notfound"})
    assert r["ok"] is False
    assert "404" in r["result"]["error"]


def test_fetch_url_connection_refused_is_failure(reg):
    # port 1 should be closed on basically every machine
    r = reg.invoke("fetch_url", {"url": "http://127.0.0.1:1/", "timeout": 2})
    assert r["ok"] is False
