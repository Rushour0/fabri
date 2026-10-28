"""ToolRegistry checks every call before it runs: required arguments, and a
human approver for `always_approve` tools. It also counts usage per tool.
Python tool sources honor their timeout_s."""
from __future__ import annotations

import sys
import time
import types

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ToolRegistry
from fabri.tools.result import tool_error, tool_ok
from fabri.tools.sources import build_tool_source
from fabri.tools.policy import ToolPolicy


def manifest(name: str, approval: str | None = None, required: list[str] | None = None) -> ToolManifest:
    schema = {"type": "object", "properties": {"to": {"type": "string"}}, "required": required or []}
    return ToolManifest(name=name, description=name, command=[], input_schema=schema,
                        output_schema={"type": "object"}, approval=approval)


def registry(**kwargs) -> tuple[ToolRegistry, list[dict]]:
    calls: list[dict] = []
    reg = ToolRegistry([], **kwargs)
    reg.register_callable(manifest("send_email", "always_approve", ["to"]),
                          lambda a: calls.append(a) or tool_ok({"sent": True}))
    reg.register_callable(manifest("list_items", "read"), lambda a: tool_ok({"items": []}))
    reg.register_callable(manifest("flaky"), lambda a: tool_error("upstream 500"))
    return reg, calls


def test_always_approve_refused_without_approver():
    reg, calls = registry()
    out = reg.invoke("send_email", {"to": "a@b.c"})
    assert not out["ok"] and "needs human approval" in out["error"] and calls == []


def test_approver_decides():
    seen = []

    def approver(name, args, m):
        seen.append((name, args["to"], m.approval))
        return args["to"].endswith("@ok.test")

    reg, calls = registry(approver=approver)
    assert reg.invoke("send_email", {"to": "x@ok.test"})["ok"]
    denied = reg.invoke("send_email", {"to": "x@evil.test"})
    assert not denied["ok"] and "declined" in denied["error"]
    assert calls == [{"to": "x@ok.test"}]
    assert seen == [("send_email", "x@ok.test", "always_approve"), ("send_email", "x@evil.test", "always_approve")]


def test_approver_that_raises_blocks_the_call():
    def approver(*_):
        raise RuntimeError("queue down")

    reg, calls = registry(approver=approver)
    out = reg.invoke("send_email", {"to": "x@ok.test"})
    assert not out["ok"] and "approval check failed" in out["error"] and calls == []


def test_read_tools_skip_the_approver():
    reg, _ = registry(approver=lambda *_: False)
    assert reg.invoke("list_items", {})["ok"]


def test_missing_required_argument_is_refused_before_the_call():
    reg, calls = registry(approver=lambda *_: True)
    out = reg.invoke("send_email", {})
    assert not out["ok"] and "missing required argument(s): to" in out["error"] and calls == []
    assert not reg.invoke("list_items", ["not", "a", "dict"])["ok"]


def test_batch_goes_through_the_gate():
    reg, calls = registry()
    reg.register_callable(manifest("batch"), lambda a: tool_ok({}))
    out = reg.invoke("batch", {"calls": [{"name": "send_email", "args": {"to": "a@b.c"}},
                                         {"name": "list_items", "args": {}}]})
    first, second = out["result"]["results"]
    assert not first["ok"] and second["ok"] and calls == []


def test_usage_counts_calls_failures_and_refusals():
    reg, _ = registry()
    reg.invoke("list_items", {})
    reg.invoke("list_items", {})
    reg.invoke("flaky", {})
    reg.invoke("send_email", {"to": "a@b.c"})
    assert reg.usage["list_items"]["calls"] == 2 and reg.usage["list_items"]["failures"] == 0
    assert reg.usage["flaky"]["failures"] == 1
    assert reg.usage["send_email"] == {"calls": 1, "failures": 0, "refused": 1, "seconds": 0.0}
    assert reg.usage["list_items"]["seconds"] >= 0


def test_python_source_times_out():
    module = types.ModuleType("fabri_test_slow_tools")

    def slow(args):
        time.sleep(2)
        return {"done": True}

    module.slow = slow
    sys.modules[module.__name__] = module
    try:
        [(m, handler)] = build_tool_source(
            {"kind": "python", "name": "slow", "callable": f"{module.__name__}:slow", "timeout_s": 0.2},
            policy=ToolPolicy(allowed_kinds=("python",), python_modules=(module.__name__,)))
        t0 = time.monotonic()
        out = handler({})
        assert not out["ok"] and "timed out after 0.2s" in out["error"]
        assert time.monotonic() - t0 < 1.5
        assert m.timeout_s == 0.2
    finally:
        del sys.modules[module.__name__]
