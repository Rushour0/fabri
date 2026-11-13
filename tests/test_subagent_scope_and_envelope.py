"""Per-spawn tool scope (D1) and the capped child envelope (D2)."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fabri.core.agent import _index_parallel_groups, _one_writer_per_group
from fabri.core.llm import ToolCall
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry
from fabri.tools.subagent_envelope import TRUNCATION_NOTE, cap_envelope
from fabri.tools.tool_scope import (
    PARENT_TOOLS_ENV,
    filter_to_parent,
    narrow_registry,
    normalize_scope,
    scope_is_read_only,
)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"


def _manifest(name: str, approval: str | None = None) -> ToolManifest:
    return ToolManifest(
        name=name, description=name,
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({'x': 1}))"],
        input_schema={"type": "object", "properties": {}}, output_schema={"type": "object"},
        approval=approval,
    )


def _registry(*manifests: ToolManifest, approver=None) -> ToolRegistry:
    reg = ToolRegistry([], approver=approver)
    for m in manifests:
        reg.register(m)
    return reg


# --- D1: scope narrows, never widens -------------------------------------

def test_scope_narrows_to_listed_tools():
    reg = _registry(_manifest("read_file"), _manifest("write_file"), _manifest("bash"))
    report = narrow_registry(reg, ["read_file", "bash"])
    assert sorted(reg.tools) == ["bash", "read_file"]
    assert report["kept"] == ["bash", "read_file"]


def test_scope_never_adds_a_tool_the_config_lacks():
    reg = _registry(_manifest("read_file"))
    report = narrow_registry(reg, ["read_file", "deploy_prod"])
    assert sorted(reg.tools) == ["read_file"]
    assert report["unmatched"] == ["deploy_prod"]


def test_read_only_preset_keeps_read_class_tools_only():
    reg = _registry(_manifest("read_file"), _manifest("grep"), _manifest("write_file"),
                    _manifest("crm_lookup", approval="read"), _manifest("crm_update", approval="write"),
                    _manifest("grep_override", approval="write"))
    narrow_registry(reg, ["read_only"])
    assert sorted(reg.tools) == ["crm_lookup", "grep", "read_file"]


def test_manifest_approval_beats_builtin_name_list():
    # A tool named like a read builtin but marked write is not read.
    assert not scope_is_read_only(["read_file"], {"read_file": _manifest("read_file", approval="write")})


def test_no_scope_leaves_registry_alone_and_empty_scope_removes_all():
    reg = _registry(_manifest("read_file"), _manifest("bash"))
    assert narrow_registry(reg, None) == {}
    assert len(reg.tools) == 2
    narrow_registry(reg, [])
    assert reg.tools == {}


def test_narrowing_keeps_the_approval_gate():
    """A scoped child keeps its approver: an always_approve tool left in scope
    is still refused when there is no human to ask."""
    reg = _registry(_manifest("send_email", approval="always_approve"), _manifest("bash"))
    narrow_registry(reg, ["send_email"])
    out = reg.invoke("send_email", {})
    assert out["ok"] is False and "needs human approval" in out["error"]

    asked = []
    reg2 = _registry(_manifest("send_email", approval="always_approve"),
                     approver=lambda n, a, m: asked.append(n) or False)
    narrow_registry(reg2, ["send_email"])
    out = reg2.invoke("send_email", {})
    assert asked == ["send_email"] and "declined" in out["error"]


def test_normalize_scope_shapes():
    assert normalize_scope(None) is None
    assert normalize_scope("read_only") == ["read_only"]
    assert normalize_scope(["a", "a", " b ", 3, ""]) == ["a", "b"]
    assert normalize_scope({"a": 1}) is None


def test_filter_to_parent_drops_names_the_parent_lacks():
    kept, dropped = filter_to_parent(["read_file", "bash", "read_only"], {"read_file"})
    assert kept == ["read_file", "read_only"] and dropped == ["bash"]
    assert filter_to_parent(["x"], None) == (["x"], [])


def test_build_runner_command_threads_filtered_scope(monkeypatch):
    from fabri.tools.examples.spawn_subagent import build_runner_command

    monkeypatch.setenv(PARENT_TOOLS_ENV, json.dumps(["read_file", "grep"]))
    cmd = build_runner_command({"config_path": "/tmp/x.yaml", "task": "t",
                                "tool_scope": ["read_file", "write_file"]})
    assert json.loads(cmd[cmd.index("--tool-scope") + 1]) == ["read_file"]


def test_build_runner_command_refuses_scope_with_nothing_left(monkeypatch):
    from fabri.tools.examples.spawn_subagent import build_runner_command

    monkeypatch.setenv(PARENT_TOOLS_ENV, json.dumps(["read_file"]))
    with pytest.raises(ValueError, match="names no tool the parent has"):
        build_runner_command({"config_path": "/tmp/x.yaml", "task": "t", "tool_scope": ["bash"]})


def test_build_runner_command_without_scope_has_no_flag():
    from fabri.tools.examples.spawn_subagent import build_runner_command

    assert "--tool-scope" not in build_runner_command({"config_path": "/tmp/x.yaml", "task": "t"})


def test_registry_hands_parent_tool_names_to_spawn_only():
    seen = {}

    class _Sandbox:
        def run_tool(self, manifest, args, extra_env=None):
            seen[manifest.name] = dict(extra_env or {})
            return {"ok": True, "result": {}}

    reg = ToolRegistry([], sandbox=_Sandbox())
    reg.register(_manifest("spawn_subagent"))
    reg.register(_manifest("read_file"))
    reg.invoke("spawn_subagent", {})
    reg.invoke("read_file", {})
    assert json.loads(seen["spawn_subagent"][PARENT_TOOLS_ENV]) == ["read_file", "spawn_subagent"]
    assert PARENT_TOOLS_ENV not in seen["read_file"]


def test_spawn_manifest_is_a_write_and_declares_tool_scope():
    m = ToolManifest.from_file(EXAMPLES_DIR / "spawn_subagent.json")
    assert m.approval == "write"
    assert m.input_schema["properties"]["tool_scope"]["type"] == "array"


# --- one writer at a time -------------------------------------------------

def _spawn(group, scope=None):
    args = {"task": "t", "parallel_group": group}
    if scope is not None:
        args["tool_scope"] = scope
    return ToolCall(name="spawn_subagent", args=args, id=None)


def test_one_writer_per_parallel_group():
    reg = _registry(_manifest("read_file"), _manifest("write_file"))
    calls = [_spawn("g", ["read_only"]), _spawn("g", ["write_file"]),
             _spawn("g", ["read_file"]), _spawn("g", ["write_file", "read_file"])]
    groups = _one_writer_per_group(_index_parallel_groups(calls), calls, reg)
    # The second writer (index 3) leaves the group and runs serially.
    assert groups == {"g": [0, 1, 2]}


def test_unscoped_spawns_keep_legacy_fan_out():
    reg = _registry(_manifest("write_file"))
    calls = [_spawn("g"), _spawn("g"), _spawn("g")]
    assert _one_writer_per_group(_index_parallel_groups(calls), calls, reg) == {"g": [0, 1, 2]}


# --- D2: capped envelope --------------------------------------------------

def test_cap_envelope_truncates_summary_and_keeps_pointers():
    env = {"final_text": "word " * 20000, "outcome": "success", "session_id": "s1",
           "trace_path": "/t/s1.jsonl", "usage": {"total_cost_usd": 0.1}}
    out = cap_envelope(env, 1500)
    assert out["summary_truncated"] is True
    assert out["final_text"].endswith(TRUNCATION_NOTE)
    assert out["final_text_tokens"] > 1500
    assert out["session_id"] == "s1" and out["trace_path"] == "/t/s1.jsonl"
    assert out["usage"] == {"total_cost_usd": 0.1}
    # Roughly the budget: well under the original 20k words.
    assert len(out["final_text"]) < 1600 * 6
    json.loads(json.dumps(out))  # envelope stays valid JSON


def test_cap_envelope_leaves_short_output_alone():
    env = {"final_text": "done", "structured_output": {"a": 1}, "session_id": "s", "trace_path": "p"}
    out = cap_envelope(env, 2000)
    assert out["final_text"] == "done" and out["structured_output"] == {"a": 1}
    assert out["summary_truncated"] is False


def test_cap_envelope_drops_oversized_structured_output():
    env = {"final_text": "ok", "structured_output": {"rows": ["x" * 50] * 2000}, "session_id": "s",
           "trace_path": "p"}
    out = cap_envelope(env, 500)
    assert out["structured_output"] is None and out["structured_output_dropped_tokens"] > 500


def test_cap_disabled_with_zero():
    env = {"final_text": "word " * 5000}
    assert cap_envelope(env, 0) == env


def test_runner_byte_cap_reports_cause_instead_of_malformed_json(monkeypatch):
    from fabri.tools import runner

    monkeypatch.setattr(runner, "RUNNER_OUTPUT_CAP_BYTES", 100)
    m = ToolManifest(
        name="noisy", description="noisy",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print(json.dumps({'t': 'x'*500}))"],
        input_schema={"type": "object"}, output_schema={"type": "object"},
    )
    out = runner.run_tool(m, {})
    assert out["ok"] is False and out["truncated"] is True
    assert "exceeded 100 bytes" in out["error"] and out["preview"].startswith('{"t"')


def _run_spawn(stdin: str, fake_runner: Path, env_extra: dict | None = None):
    shim = textwrap.dedent(f"""
        import fabri.tools.examples.spawn_subagent as s
        from pathlib import Path
        s.RUNNER_SCRIPT = Path({str(fake_runner)!r})
        import sys as _sys
        _sys.exit(s.main())
    """)
    env = os.environ.copy()
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", shim], input=stdin, capture_output=True, text=True, env=env)


def test_spawn_caps_an_uncapped_child_and_forwards_scope(tmp_path):
    runner = tmp_path / "fake_runner.py"
    runner.write_text(textwrap.dedent("""
        import json, sys
        sys.stdin.read()
        scope = sys.argv[sys.argv.index("--tool-scope") + 1] if "--tool-scope" in sys.argv else None
        print(json.dumps({"final_text": "word " * 30000, "outcome": "success",
                          "session_id": "c1", "trace_path": "/tmp/c1.jsonl", "scope": scope}))
    """))
    cfg = tmp_path / "agent.yaml"
    cfg.write_text("# unused\n")
    proc = _run_spawn(json.dumps({"config_path": str(cfg), "task": "go", "tool_scope": ["read_only", "bash"]}),
                      runner, {PARENT_TOOLS_ENV: json.dumps(["read_file"])})
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["summary_truncated"] is True and out["session_id"] == "c1"
    assert json.loads(out["scope"]) == ["read_only"]


def test_spawn_does_not_recap_a_runner_capped_envelope(tmp_path):
    runner = tmp_path / "fake_runner.py"
    runner.write_text(textwrap.dedent("""
        import json, sys
        sys.stdin.read()
        print(json.dumps({"final_text": "word " * 3000, "outcome": "success", "summary_truncated": False,
                          "session_id": "c2", "trace_path": "/tmp/c2.jsonl"}))
    """))
    cfg = tmp_path / "agent.yaml"
    cfg.write_text("# unused\n")
    proc = _run_spawn(json.dumps({"config_path": str(cfg), "task": "go"}), runner)
    out = json.loads(proc.stdout)
    # The child's own config chose its cap (here: none hit); spawn respects it.
    assert out["final_text"] == "word " * 3000
