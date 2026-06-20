"""S1 -- fabri.sandbox package. Verify the ABC + LocalSandbox preserve
today's behavior (the existing 169 tests already cover real tool execution
through the registry; these tests pin the seam itself).
"""
from pathlib import Path

import pytest

from fabri.sandbox import LocalSandbox, Sandbox
from fabri.tools.registry import ToolRegistry

EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
)


def test_registry_defaults_to_local_sandbox():
    reg = ToolRegistry(EXAMPLES_DIR)
    assert isinstance(reg.sandbox, LocalSandbox)


def test_registry_accepts_custom_sandbox():
    """A consuming service plugs in DockerSandbox (S2) or a test stub the
    same way; we exercise that seam with a stub."""

    calls = []

    class StubSandbox(Sandbox):
        def run_tool(self, manifest, payload, extra_env=None):
            calls.append((manifest.name, payload, extra_env))
            return {"ok": True, "result": {"stub": True}}

    reg = ToolRegistry(EXAMPLES_DIR, sandbox=StubSandbox())
    result = reg.invoke("read_file", {"path": "ignored"})

    assert result == {"ok": True, "result": {"stub": True}}
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert calls[0][1] == {"path": "ignored"}


def test_sandbox_root_still_threaded_via_extra_env(tmp_path):
    """LocalSandbox preserves the FABRI_SANDBOX_ROOT threading that
    file/shell tools rely on. The end-to-end check is the existing
    `test_unit_file_tools` suite; here we just confirm the registry sets
    extra_env when sandbox_root is configured."""
    seen = {}

    class CaptureSandbox(Sandbox):
        def run_tool(self, manifest, payload, extra_env=None):
            seen["extra_env"] = extra_env
            return {"ok": True, "result": {}}

    reg = ToolRegistry(EXAMPLES_DIR, sandbox_root=str(tmp_path), sandbox=CaptureSandbox())
    reg.invoke("read_file", {"path": "x"})
    assert seen["extra_env"] == {"FABRI_SANDBOX_ROOT": str(tmp_path)}


def test_default_sync_methods_are_no_ops():
    """LocalSandbox doesn't ferry state; sync_in/sync_out/dispose must not
    raise so a host service can call them unconditionally regardless of
    whether the active sandbox is Local or Docker."""
    sb = LocalSandbox()
    sb.sync_in("proj-1", Path("/tmp/nowhere"))
    sb.sync_out("proj-1", ["foo", "bar"])
    sb.dispose()


def test_abstract_sandbox_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Sandbox()  # type: ignore[abstract]
