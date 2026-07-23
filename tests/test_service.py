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
from fabri.service.service import (
    FabriService,
    PersistedRunUnavailableError,
    serve_stdio,
)
from fabri.service.tailer import extract_cost, run_trace_path, tail_events


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


# Prints the result envelope FIRST, then a human-readable trailer on stdout --
# reproducing the real `fabri run`, which emitted a "Synthesized N guideline(s)"
# note after the JSON (that trailer now goes to stderr, but the parser must be
# robust to any stdout trailer regardless).
_FAKE_AGENT_WITH_TRAILER = _FAKE_AGENT + """
print("\\nSynthesized 1 guideline(s) from this run:")
print("  [success_pattern] Do the thing.")
"""


@pytest.fixture
def fake_agent_with_trailer(tmp_path: Path) -> Path:
    p = tmp_path / "fake_agent_trailer.py"
    p.write_text(textwrap.dedent(_FAKE_AGENT_WITH_TRAILER))
    return p


def _builder_for(script: Path):
    def _build(task, config_path, session_id, fabri_home):
        return [sys.executable, str(script)]
    return _build


def _persist_finished_run(service: FabriService, session_id: str) -> list[dict]:
    """Fabricate the durable store row + trace left by a finished run."""
    events = [
        {"type": "start", "task": "persisted task"},
        {"type": "tool_call", "name": "noop", "ok": True},
        {"type": "final", "text": "persisted answer", "outcome": "success"},
        {
            "type": "usage",
            "input_tokens": 12,
            "output_tokens": 7,
            "cost_usd": 0.002,
            "subagent_cost_usd": 0.001,
            "total_cost_usd": 0.003,
        },
        {"type": "post_run_usage", "cost_usd": 0.0002},
    ]
    trace_path = run_trace_path(service.home_root / session_id, session_id)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    service.run_store.record_submit(
        session_id=session_id,
        agency="default",
        task="persisted task",
        submitted_at=1.0,
    )
    service.run_store.record_terminal(
        session_id=session_id,
        finished_at=2.0,
        event="result",
        outcome="success",
        cost=extract_cost(events),
    )
    return events


# A fake that writes a `start` event then sleeps well past a test's patience, so
# a cancel arrives while it is genuinely running (mirrors a long agent loop).
_FAKE_AGENT_SLEEPY = """
import json, os, time
from pathlib import Path

home = Path(os.environ["FABRI_HOME"])
sid = os.environ["FABRI_SESSION_ID"]
trace = home / ".fabri" / "traces" / (sid + ".jsonl")
trace.parent.mkdir(parents=True, exist_ok=True)
with trace.open("a") as f:
    f.write(json.dumps({"type": "start", "task": "t"}) + "\\n")
    f.flush()
time.sleep(60)
"""


@pytest.fixture
def fake_agent_sleepy(tmp_path: Path) -> Path:
    p = tmp_path / "fake_agent_sleepy.py"
    p.write_text(textwrap.dedent(_FAKE_AGENT_SLEEPY))
    return p


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
                    "total_cost_usd": 0.0, "post_run_cost_usd": 0.0,
                    "cost_by_model": {}, "metrics": {}}


def test_extract_cost_surfaces_cost_by_model_and_metrics():
    """The usage event already carries a per-model COGS breakdown + run metrics;
    the cost surface must pass them through so a UI can render them."""
    events = [
        {
            "type": "usage",
            "cost_usd": 0.03,
            "subagent_cost_usd": 0.01,
            "total_cost_usd": 0.04,
            "cost_by_model": {"claude-sonnet-5": 0.02, "claude-haiku-4-5": 0.01},
            "input_tokens": 1200,
            "output_tokens": 300,
            "step_count": 4,
            "wall_time_s": 8.1,
            "subagent_count": 2,
            "subagent_failed_count": 0,
        },
    ]
    cost = extract_cost(events)
    assert cost["cost_by_model"] == {"claude-sonnet-5": 0.02, "claude-haiku-4-5": 0.01}
    assert cost["metrics"]["input_tokens"] == 1200
    assert cost["metrics"]["step_count"] == 4
    assert cost["metrics"]["wall_time_s"] == 8.1
    assert cost["metrics"]["subagent_count"] == 2
    # Keys absent from the usage event stay out of metrics (not zero-defaulted).
    assert "guideline_reuse_rate" not in cost["metrics"]


# --- 3. launcher -------------------------------------------------------------

def test_build_run_command_argv():
    cmd = build_run_command("do the thing", "/tmp/run.yaml", "sess-1")
    # --config is a global option: it must precede the `run` subcommand, else
    # argparse rejects it (exit 2). Regression guard for that ordering.
    assert cmd[1:] == ["-m", "fabri.cli", "--config", "/tmp/run.yaml",
                       "run", "do the thing", "--session-id", "sess-1"]


def test_build_run_command_is_accepted_by_the_real_cli(tmp_path: Path):
    """The argv from build_run_command must actually parse — every other service
    test stubs the command, so this is the only guard that the real `fabri run`
    accepts it. `--dry-run` exits 0 with no API key and no LLM call."""
    import subprocess

    cfg = tmp_path / "agent.yaml"
    cfg.write_text("memory:\n  backend: sqlite\n  collection: t\n"
                   "llm:\n  provider: anthropic\n  model: claude-haiku-4-5\n")
    cmd = build_run_command("hello", cfg, "sess-dry") + ["--dry-run"]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 0, f"real CLI rejected argv: {proc.stderr[-500:]}"


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


def test_service_result_survives_stdout_trailer(tmp_path: Path, fake_agent_with_trailer: Path):
    """A successful run whose stdout has a human-readable trailer after the JSON
    envelope must still parse to success -- else `fabri serve` reports every
    guideline-synthesizing run as a failure (agent stdout was not JSON)."""
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent_with_trailer),
    )
    session_id = svc.submit("go")
    result = svc.result(session_id, timeout=30)
    assert result["success"] is True
    assert result["outcome"] == "success"
    assert result["final_text"] == "all done"
    assert result.get("error") is None
    svc.close()


def test_service_unknown_session_raises(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    with pytest.raises(KeyError, match="unknown session_id"):
        svc.stream("nope")
    with pytest.raises(KeyError, match="unknown session_id"):
        svc.result("nope")


# --- Slice 0: cancel, session index / history, persistence -------------------

def test_service_list_sessions_records_finished_run(tmp_path: Path, fake_agent: Path):
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    session_id = svc.submit("build it", thread_id="thread-1")
    # Before terminal: the run shows up as running with its task + grouping.
    live = {s["session_id"]: s for s in svc.list_sessions()}
    assert live[session_id]["task"] == "build it"
    assert live[session_id]["thread_id"] == "thread-1"
    # After result(): status resolves to done and cost is recorded in history.
    svc.result(session_id, timeout=30)
    done = {s["session_id"]: s for s in svc.list_sessions()}[session_id]
    assert done["status"] == "done"
    assert done["outcome"] == "success"
    assert done["cost"]["total_cost_usd"] == 0.0015
    svc.close()


def test_service_history_survives_restart(tmp_path: Path, fake_agent: Path):
    """History is re-derived from the on-disk index, not RAM: a fresh service on
    the same home_root still lists a run finished by a prior instance."""
    home = tmp_path / "runs"
    svc = FabriService(home_root=home, command_builder=_builder_for(fake_agent))
    session_id = svc.submit("go")
    svc.result(session_id, timeout=30)
    svc.close()

    reborn = FabriService(home_root=home, command_builder=_builder_for(fake_agent))
    sessions = {s["session_id"]: s for s in reborn.list_sessions()}
    assert session_id in sessions
    assert sessions[session_id]["status"] == "done"
    assert sessions[session_id]["cost"]["total_cost_usd"] == 0.0015


def test_service_stream_replays_persisted_trace_after_restart(tmp_path: Path):
    home = tmp_path / "runs"
    original = FabriService(home_root=home)
    session_id = "persisted-stream"
    expected = _persist_finished_run(original, session_id)
    original.close()

    reborn = FabriService(home_root=home)
    assert list(reborn.stream(session_id, timeout=0.1)) == expected
    reborn.close()


def test_service_result_reconstructs_envelope_after_restart(tmp_path: Path):
    home = tmp_path / "runs"
    original = FabriService(home_root=home)
    session_id = "persisted-result"
    _persist_finished_run(original, session_id)
    original.close()

    reborn = FabriService(home_root=home)
    assert reborn.result(session_id) == {
        "session_id": session_id,
        "success": True,
        "outcome": "success",
        "final_text": "persisted answer",
        "structured_output": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "cost_usd": 0.002,
            "subagent_cost_usd": 0.001,
            "total_cost_usd": 0.003,
        },
        "cost": {
            "cost_usd": 0.002,
            "subagent_cost_usd": 0.001,
            "total_cost_usd": 0.003,
            "post_run_cost_usd": 0.0002,
            "cost_by_model": {},
            "metrics": {"input_tokens": 12, "output_tokens": 7},
        },
        "error": None,
    }
    assert reborn.cancel(session_id) == {
        "session_id": session_id,
        "status": "already_ended",
    }
    reborn.close()


def test_service_known_run_without_trace_has_typed_error(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    svc.run_store.record_submit(
        session_id="missing-trace",
        agency="default",
        task="gone",
        submitted_at=1.0,
    )
    with pytest.raises(PersistedRunUnavailableError, match="trace is unavailable"):
        svc.result("missing-trace")
    svc.close()


def test_service_cancel_terminates_running_run(tmp_path: Path, fake_agent_sleepy: Path):
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent_sleepy),
    )
    session_id = svc.submit("long task")
    # Wait for the `start` event so we know the child is actually up.
    for ev in svc.stream(session_id, timeout=30):
        if ev.get("type") == "start":
            break
    out = svc.cancel(session_id)
    assert out["status"] == "cancelled"
    # The child process is gone and history records the cancellation.
    handle = svc._handle(session_id)
    handle.wait(timeout=30)
    assert not handle.is_running()
    cancelled = {s["session_id"]: s for s in svc.list_sessions()}[session_id]
    assert cancelled["status"] == "cancelled"
    svc.close()


def test_service_cancel_finished_run_is_noop(tmp_path: Path, fake_agent: Path):
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    session_id = svc.submit("go")
    svc.result(session_id, timeout=30)
    out = svc.cancel(session_id)
    assert out["status"] == "already_ended"
    # A cancel after a normal finish doesn't overwrite the recorded outcome.
    assert {s["session_id"]: s for s in svc.list_sessions()}[session_id]["status"] == "done"
    svc.close()


def test_service_cancel_unknown_session_raises(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    with pytest.raises(KeyError):
        svc.cancel("nope")


# --- Slice 3: fleet fan-out + roll-up ----------------------------------------

def test_submit_fleet_fans_out_and_rolls_up_cost(tmp_path: Path, fake_agent: Path):
    svc = FabriService(
        home_root=tmp_path / "runs",
        command_builder=_builder_for(fake_agent),
    )
    fleet = svc.submit_fleet(
        [
            {"task": "account A", "label": "acme"},
            {"task": "account B", "label": "globex"},
            {"task": "account C", "label": "initech"},
        ]
    )
    fleet_id = fleet["fleet_id"]
    assert len(fleet["sessions"]) == 3
    # Drive each member to completion so its cost lands in the index.
    for s in fleet["sessions"]:
        svc.result(s["session_id"], timeout=30)

    status = svc.fleet_status(fleet_id)
    assert status["counts"]["done"] == 3
    # Each fake run bills 0.0015; the fleet total is the exact per-session sum.
    assert status["totals"]["total_cost_usd"] == pytest.approx(0.0045)
    # Members carry their human label for the drill-down list.
    labels = {m["label"] for m in status["sessions"]}
    assert labels == {"acme", "globex", "initech"}
    svc.close()


def test_list_fleets_groups_and_survives_restart(tmp_path: Path, fake_agent: Path):
    home = tmp_path / "runs"
    svc = FabriService(home_root=home, command_builder=_builder_for(fake_agent))
    fleet = svc.submit_fleet([{"task": "a"}, {"task": "b"}])
    for s in fleet["sessions"]:
        svc.result(s["session_id"], timeout=30)
    svc.close()

    reborn = FabriService(home_root=home, command_builder=_builder_for(fake_agent))
    fleets = reborn.list_fleets()
    assert len(fleets) == 1
    assert fleets[0]["fleet_id"] == fleet["fleet_id"]
    assert fleets[0]["size"] == 2
    assert fleets[0]["totals"]["total_cost_usd"] == pytest.approx(0.003)


def test_submit_fleet_rejects_item_without_task(tmp_path: Path, fake_agent: Path):
    svc = FabriService(home_root=tmp_path / "runs", command_builder=_builder_for(fake_agent))
    with pytest.raises(ValueError):
        svc.submit_fleet([{"label": "no-task"}])
    svc.close()


def test_fleet_status_unknown_raises(tmp_path: Path):
    svc = FabriService(home_root=tmp_path / "runs")
    with pytest.raises(KeyError):
        svc.fleet_status("nope")


def test_http_fleet_submit_and_status(tmp_path: Path, fake_agent: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(home_root=tmp_path / "runs", command_builder=_builder_for(fake_agent))
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _http_post(
            host, port, "/fleets",
            {"items": [{"task": "x", "label": "one"}, {"task": "y", "label": "two"}]},
        )
        assert status == 200
        fleet = json.loads(body)
        assert len(fleet["sessions"]) == 2
        # drive members to completion
        for s in fleet["sessions"]:
            _http_get(host, port, f"/runs/{s['session_id']}/events")

        status, body = _http_get(host, port, f"/fleets/{fleet['fleet_id']}")
        assert status == 200
        roll = json.loads(body)
        assert roll["counts"]["done"] == 2
        assert roll["totals"]["total_cost_usd"] == pytest.approx(0.003)

        # list + missing-items + unknown-fleet
        status, body = _http_get(host, port, "/fleets")
        assert status == 200 and len(json.loads(body)["fleets"]) == 1
        status, _ = _http_post(host, port, "/fleets", {"items": []})
        assert status == 400
        status, _ = _http_get(host, port, "/fleets/nope")
        assert status == 404
    finally:
        server.shutdown()
        svc.close()


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


def _http_post(conn_host, port, path, payload):
    conn = http.client.HTTPConnection(conn_host, port, timeout=30)
    conn.request(
        "POST", path, body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def test_http_list_runs_and_cancel(tmp_path: Path, fake_agent: Path, fake_agent_sleepy: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(home_root=tmp_path / "runs")
    # Route each task to the right fake: normal one finishes, sleepy one hangs
    # so we can cancel it over HTTP.
    def builder(task, config_path, session_id, fabri_home):
        script = fake_agent_sleepy if task == "hang" else fake_agent
        return [sys.executable, str(script)]
    svc.command_builder = builder

    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # a finished run + a hanging run
        status, body = _http_post(host, port, "/runs", {"task": "quick"})
        assert status == 200
        quick_id = json.loads(body)["session_id"]
        _http_get(host, port, f"/runs/{quick_id}/events")  # drive to completion

        status, body = _http_post(host, port, "/runs", {"task": "hang"})
        hang_id = json.loads(body)["session_id"]
        # wait for the sleepy child's start event so cancel hits a live process
        for ev in svc.stream(hang_id, timeout=30):
            if ev.get("type") == "start":
                break

        # GET /runs lists both
        status, body = _http_get(host, port, "/runs")
        assert status == 200
        sessions = {s["session_id"]: s for s in json.loads(body)["sessions"]}
        assert sessions[quick_id]["status"] == "done"
        assert hang_id in sessions

        # POST cancel the hanging one
        status, body = _http_post(host, port, f"/runs/{hang_id}/cancel", {})
        assert status == 200 and json.loads(body)["status"] == "cancelled"

        # cancel an unknown session -> 404
        status, _ = _http_post(host, port, "/runs/does-not-exist/cancel", {})
        assert status == 404
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


def test_http_unknown_run_events_and_result_are_json_404(tmp_path: Path):
    from fabri.service.http_server import serve_http

    svc = FabriService(home_root=tmp_path / "runs")
    server = serve_http(svc, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        expected = {"error": "unknown session_id 'does-not-exist'"}
        status, body = _http_get(host, port, "/runs/does-not-exist/events")
        assert status == 404
        assert json.loads(body) == expected

        status, body = _http_get(host, port, "/runs/does-not-exist/result")
        assert status == 404
        assert json.loads(body) == expected
    finally:
        server.shutdown()
        svc.close()
