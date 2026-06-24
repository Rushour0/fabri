"""Trace event rendering, extracted from cli.py into orchestrator.trace_render
so it can be exercised without the argparse layer."""
from fabri.orchestrator.trace_render import format_payload, render_event, wrap_block


def test_render_tool_call_shows_args_and_result():
    ev = {"type": "tool_call", "ts": 10.0, "name": "read_file",
          "args": {"path": "a.txt"}, "result": {"ok": True, "result": "hi"}}
    out = render_event(ev, t0=10.0)
    assert "tool_call read_file" in out
    assert "ok=True" in out
    assert "args:" in out and "result:" in out
    assert '"path": "a.txt"' in out


def test_render_tool_call_parallel_tag():
    ev = {"type": "tool_call", "ts": 1.0, "name": "spawn_subagent",
          "args": {}, "result": {"ok": False, "error": "x"},
          "parallel_group": "g1"}
    out = render_event(ev, t0=0.0)
    assert "[g1]" in out
    assert "ok=False" in out


def test_render_step_markers_and_final():
    assert "step 2" in render_event({"type": "step_started", "ts": 0, "step": 2}, 0)
    fin = render_event({"type": "final", "ts": 0, "outcome": "success", "text": "done"}, 0)
    assert "final outcome=success" in fin and "done" in fin


def test_render_unknown_kind_is_lossy_but_safe():
    out = render_event({"type": "mystery", "ts": 0, "blob": "x" * 500}, 0)
    assert out.startswith("  ")
    assert "mystery" in out
    # Tail is capped so a giant unknown event can't flood the viewer.
    assert len(out) < 300


def test_format_payload_truncates():
    big = {"items": list(range(200))}
    out = format_payload(big, max_lines=10)
    assert "truncated" in out
    assert len(out.splitlines()) == 11  # 10 + the truncation note


def test_wrap_block_preserves_blank_lines():
    lines = wrap_block("a\n\nb", indent="  ", width=80).splitlines()
    assert lines == ["  a", "  ", "  b"]
