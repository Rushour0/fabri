"""B7 -- self-contained `fabri serve` service. Fully offline: no network, no
real LLM, no Qdrant. The agent subprocess is stubbed by a tiny fake script that
writes a known JSONL trace and prints a result envelope -- mirroring how
`test_spawn_subagent.py` stubs the runner with a per-test fake script.

Coverage:
1. binding: per-run overrides deep-merge onto the template (and the written
   run.yaml round-trips through the real config loader).
2. tailer: trace lines parse into the events vocabulary in order; cost is
   extracted from the `usage` event.
3. launcher: `build_run_command` argv plumbing.
4. end-to-end: FabriService submit -> stream events -> read final cost, via the
   fake agent script (no LLM).
5. transports: stdio JSON-lines and the HTTP/SSE server, both over the fake.
"""
import http.client
import json
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from fabri.config import load_config
from fabri.service.binding import bind_run_config, merge_overrides
from fabri.service.launcher import build_run_command, launch_run
from fabri.service.service import FabriService, serve_stdio
from fabri.service.tailer import extract_cost, tail_events


# --- fake agent script -------------------------------------------------------

# Writes a deterministic trace to $FABRI_HOME/.fabri/traces/<sid>.jsonl, then
# prints the result envelope `fabri run` would. Reads FABRI_HOME / FABRI_SESSION_ID
# from the env the launcher sets.
_FAKE_AGENT = """
import json, os
from pathlib import Path

home = Path(os.environ["FABRI_HOME"])
sid = os.environ["FABRI_SESSION_ID"]
trace = home / ".fabri" / "traces" / (sid + ".jsonl")
trace.parent.mkdir(parents=True, exist_ok=True)

events = [
    {"type": "start", "task": "t"},
    {"type": "step_started", "step": 0},
    {"type": "tool_call", "name": "noop", "ok": True},
    {"type": "final", "text": "all done", "outcome": "success"},
    {"type": "usage", "input_tokens": 10, "output_tokens": 5,
     "cost_usd": 0.0012, "subagent_cost_usd": 0.0003, "total_cost_usd": 0.0015},
    {"type": "post_run_usage", "source": "memory_compression", "cost_usd": 0.0001},
]
with trace.open("a") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\\n")

print(json.dumps({
    "session_id": sid,
    "success": True,
    "final_text": "all done",
    "structured_output": None,
    "outcome": "success",
    "usage": {"cost_usd": 0.0012, "total_cost_usd": 0.0015},
}))
"""


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    p = tmp_path / "fake_agent.py"
    p.write_text(textwrap.dedent(_FAKE_AGENT))
    return p


def _builder_for(script: Path):
    def _build(task, config_path, session_id, fabri_home):
        return [sys.executable, str(script)]
    return _build


# --- 1. binding --------------------------------------------------------------

def test_merge_overrides_deep_merges_nested():
    base = {"memory": {"collection": "base", "qdrant_url": "http://a"},
            "llm": {"model": "m1", "max_tokens": 100}}
    overrides = {"memory": {"collection": "tenant_x"}, "llm": {"model": "m2"}}
    merged = merge_overrides(base, overrides)
    # Overridden leaves change; sibling leaves under the same subtree survive.
    assert merged["memory"]["collection"] == "tenant_x"
    assert merged["memory"]["qdrant_url"] == "http://a"
    assert merged["llm"]["model"] == "m2"
    assert merged["llm"]["max_tokens"] == 100
    # Source mappings are not mutated.
    assert base["memory"]["collection"] == "base"


def test_merge_overrides_none_is_copy():
    base = {"a": 1}
    out = merge_overrides(base, None)
    assert out == {"a": 1}
    out["a"] = 2
    assert base["a"] == 1


def test_bind_run_config_roundtrips_through_loader(tmp_path: Path):
    template = tmp_path / "agent.yaml"
    template.write_text("memory:\n  collection: tmpl\n  backend: sqlite\nllm:\n  model: base-model\n")
    out = bind_run_config(
        template,
        {"memory": {"collection": "run42"}, "llm": {"model": "override-model"}},
        tmp_path / "run.yaml",
    )
    assert out.exists()
    # The written run.yaml loads through the real config path with defaults
    # applied and the overrides + untouched template keys both present.
    cfg = load_config(str(out))
    assert cfg["memory"]["collection"] == "run42"
    assert cfg["memory"]["backend"] == "sqlite"   # from template, untouched
    assert cfg["llm"]["model"] == "override-model"
    assert cfg["agent"]["max_steps"] == 10        # framework default, intact


def test_bind_run_config_empty_template_inherits_defaults(tmp_path: Path):
    out = bind_run_config(None, {"llm": {"model": "x"}}, tmp_path / "run.yaml")
    cfg = load_config(str(out))
    assert cfg["llm"]["model"] == "x"
    assert cfg["agent"]["max_steps"] == 10


# --- 2. tailer ---------------------------------------------------------------

def test_tail_events_yields_in_order_when_not_running(tmp_path: Path):
    trace = tmp_path / "t.jsonl"
    rows = [
        {"type": "start"},
        {"type": "step_started", "step": 0},
        {"type": "tool_call", "name": "noop"},
        {"type": "final", "text": "ok", "outcome": "success"},
        {"type": "usage", "cost_usd": 0.01, "total_cost_usd": 0.02},
    ]
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    # Producer already finished: drains the whole file, then stops.
    got = list(tail_events(trace, is_running=lambda: False))
    assert [e["type"] for e in got] == [r["type"] for r in rows]


def test_tail_events_skips_malformed_lines(tmp_path: Path):
    trace = tmp_path / "t.jsonl"
    trace.write_text(
        json.dumps({"type": "start"}) + "\n"
        + "{ this is not json\n"
        + json.dumps({"type": "final", "outcome": "success"}) + "\n"
    )
    got = list(tail_events(trace, is_running=lambda: False))
    assert [e["type"] for e in got] == ["start", "final"]


def test_tail_events_streams_while_running(tmp_path: Path):
    """A producer that appends across polls: events appear incrementally and the
    tailer stops once is_running flips false and the file is drained."""
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps({"type": "start"}) + "\n")
    state = {"polls": 0}

    def is_running():
        state["polls"] += 1
        # After the first drain, append a final batch, then report finished.
        if state["polls"] == 1:
            with trace.open("a") as f:
                f.write(json.dumps({"type": "final", "outcome": "success"}) + "\n")
                f.write(json.dumps({"type": "usage", "total_cost_usd": 0.5}) + "\n")
            return True
        return False

    got = list(tail_events(trace, is_running=is_running, poll_interval=0.0))
    assert [e["type"] for e in got] == ["start", "final", "usage"]


def test_tail_events_terminates_on_terminal_event_without_predicate(tmp_path: Path):
    trace = tmp_path / "t.jsonl"
    trace.write_text(
        json.dumps({"type": "start"}) + "\n"
        + json.dumps({"type": "final", "outcome": "success"}) + "\n"
    )
    got = list(tail_events(trace, is_running=None))
    assert [e["type"] for e in got] == ["start", "final"]


def test_extract_cost_from_usage_event():
    events = [
        {"type": "start"},
        {"type": "usage", "cost_usd": 0.0012, "subagent_cost_usd": 0.0003,
         "total_cost_usd": 0.0015},
        {"type": "post_run_usage", "cost_usd": 0.0001},
    ]
    cost = extract_cost(events)
    assert cost["cost_usd"] == 0.0012
    assert cost["subagent_cost_usd"] == 0.0003
    assert cost["total_cost_usd"] == 0.0015
    assert cost["post_run_cost_usd"] == 0.0001


def test_extract_cost_defaults_when_no_usage():
    cost = extract_cost([{"type": "start"}])
    assert cost == {"cost_usd": 0.0, "subagent_cost_usd": 0.0,
                    "total_cost_usd": 0.0, "post_run_cost_usd": 0.0}


# --- 3. launcher -------------------------------------------------------------

def test_build_run_command_argv():
    cmd = build_run_command("do the thing", "/tmp/run.yaml", "sess-1")
    assert cmd[1:] == ["-m", "fabri.cli", "run", "do the thing",
                       "--config", "/tmp/run.yaml", "--session-id", "sess-1"]


def test_launch_run_sets_home_and_session_env(tmp_path: Path, fake_agent: Path):
    home = tmp_path / "home"
    handle = launch_run(
        "t",
        config_path=tmp_path / "unused.yaml",
        fabri_home=home,
        session_id="sess-abc",
        command=[sys.executable, str(fake_agent)],
    )
    result = handle.result(timeout=30)
    assert result["session_id"] == "sess-abc"
    assert result["success"] is True
    # The fake wrote the trace under the launcher-provided home.
    assert handle.trace_path == home.resolve() / ".fabri" / "traces" / "sess-abc.jsonl"
    assert handle.trace_path.exists()


# --- 4 & 5. FabriService end-to-end + transports -----------------------------

def test_service_submit_stream_and_cost(tmp_path: Path, fake_agent: Path):
    svc = FabriService(
        template_config=None,
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    session_id = svc.submit("build it", overrides={"llm": {"model": "x"}})
    events = list(svc.stream(session_id, timeout=30))
    types = [e["type"] for e in events]
    # Events arrive parsed, in trace order.
    assert types[0] == "start"
    assert "tool_call" in types
    assert "final" in types and "usage" in types
    result = svc.result(session_id, timeout=30)
    assert result["success"] is True
    assert result["outcome"] == "success"
    assert result["final_text"] == "all done"
    # Cost surfaced from the usage event without importing the agent.
    assert result["cost"]["total_cost_usd"] == 0.0015
    assert result["cost"]["cost_usd"] == 0.0012
    assert result["cost"]["post_run_cost_usd"] == 0.0001
    svc.close()


def test_service_unknown_session_raises(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    with pytest.raises(KeyError):
        svc.result("nope")


def test_serve_stdio_roundtrip(tmp_path: Path, fake_agent: Path):
    import io

    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    stdin = io.StringIO(json.dumps({"task": "go", "overrides": {}}) + "\n")
    stdout = io.StringIO()
    serve_stdio(svc, stdin=stdin, stdout=stdout)

    lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    assert lines[0]["status"] == "submitted"
    event_types = [l["event"]["type"] for l in lines if "event" in l]
    assert "start" in event_types and "usage" in event_types
    final = lines[-1]
    assert "result" in final
    assert final["result"]["cost"]["total_cost_usd"] == 0.0015
    svc.close()


def test_serve_stdio_rejects_missing_task(tmp_path: Path):
    import io

    svc = FabriService(home_root=tmp_path / "runs")
    stdin = io.StringIO(json.dumps({"overrides": {}}) + "\n")
    stdout = io.StringIO()
    serve_stdio(svc, stdin=stdin, stdout=stdout)
    line = json.loads(stdout.getvalue().splitlines()[0])
    assert "missing required field" in line["error"]


def _http_get(conn_host, port, path):
    conn = http.client.HTTPConnection(conn_host, port, timeout=30)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def test_http_transport_submit_stream_cost(tmp_path: Path, fake_agent: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # health
        status, body = _http_get(host, port, "/health")
        assert status == 200 and json.loads(body)["status"] == "ok"

        # submit
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("POST", "/runs", body=json.dumps({"task": "go"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        submit = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        session_id = submit["session_id"]

        # stream events as SSE; the terminal frame carries the result + cost
        status, sse = _http_get(host, port, f"/runs/{session_id}/events")
        assert status == 200
        frames = [blk for blk in sse.split("\n\n") if blk.strip()]
        datas = []
        result_frame = None
        for blk in frames:
            is_result = any(line.strip() == "event: result" for line in blk.splitlines())
            data_line = next(l for l in blk.splitlines() if l.startswith("data:"))
            payload = json.loads(data_line[len("data:"):].strip())
            if is_result:
                result_frame = payload
            else:
                datas.append(payload)
        assert datas[0]["type"] == "start"
        assert result_frame is not None
        assert result_frame["cost"]["total_cost_usd"] == 0.0015
    finally:
        server.shutdown()
        svc.close()


def test_http_unknown_route_404(tmp_path: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(home_root=tmp_path / "runs")
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = _http_get(host, port, "/nope")
        assert status == 404
    finally:
        server.shutdown()
        svc.close()
