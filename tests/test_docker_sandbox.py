"""S2 -- DockerSandbox warm pool. Unit tests use a mock DockerBackend so the
suite stays Docker-free; one integration test runs against real Docker if
the `docker` CLI is present (skipped in CI without it)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fabri.sandbox import LocalSandbox, Sandbox
from fabri.sandbox.docker_sandbox import DockerBackend, DockerSandbox
from fabri.tools.manifest_schema import ToolManifest


class FakeBackend:
    """Records every call; emulates a working container exec. Used to test
    pool semantics + run_tool plumbing without touching real Docker."""

    def __init__(self, exec_returncode: int = 0, exec_stdout: str = '{"ok": 1}'):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.execs: list[dict] = []
        self.exec_returncode = exec_returncode
        self.exec_stdout = exec_stdout
        self._counter = 0

    def run_detached(self, image, *, bind_mounts, env):
        self._counter += 1
        cid = f"container-{self._counter}"
        self.started.append(cid)
        return cid

    def exec_tool(self, container_id, command, stdin, env, timeout_s):
        self.execs.append({
            "container_id": container_id,
            "command": command,
            "stdin": stdin,
            "env": env,
        })
        return self.exec_returncode, self.exec_stdout, ""

    def stop(self, container_id):
        self.stopped.append(container_id)


def _manifest() -> ToolManifest:
    return ToolManifest(
        name="noop",
        description="x",
        command=["python3", "-c", "import sys,json; sys.stdin.read(); print('{}')"],
        input_schema={}, output_schema={},
        timeout_s=10,
    )


def test_docker_sandbox_is_a_sandbox():
    backend = FakeBackend()
    sb = DockerSandbox(pool_size=1, backend=backend)
    assert isinstance(sb, Sandbox)


def test_pool_lazily_fills_on_first_acquire():
    backend = FakeBackend()
    sb = DockerSandbox(pool_size=2, backend=backend)
    assert backend.started == []
    sb.run_tool(_manifest(), {"x": 1})
    assert len(backend.started) == 1
    sb.run_tool(_manifest(), {"x": 2})  # reuses warm container
    assert len(backend.started) == 1


def test_pool_caps_at_size_under_serial_load():
    """Serial run_tool calls should reuse the same container even with a
    larger pool_size -- nothing forces a second start."""
    backend = FakeBackend()
    sb = DockerSandbox(pool_size=3, backend=backend)
    for _ in range(5):
        sb.run_tool(_manifest(), {})
    assert len(backend.started) == 1


def test_run_tool_passes_payload_via_stdin():
    backend = FakeBackend()
    sb = DockerSandbox(pool_size=1, backend=backend)
    sb.run_tool(_manifest(), {"hello": "world"})
    assert backend.execs[0]["stdin"] == json.dumps({"hello": "world"})


def test_run_tool_layers_extra_env_over_container_env():
    backend = FakeBackend()
    sb = DockerSandbox(
        pool_size=1, backend=backend,
        container_env={"BASE": "1", "FABRI_SANDBOX_ROOT": "/workspace"},
    )
    sb.run_tool(_manifest(), {}, extra_env={"FABRI_SANDBOX_ROOT": "/override"})
    env = backend.execs[0]["env"]
    assert env["BASE"] == "1"
    assert env["FABRI_SANDBOX_ROOT"] == "/override"


def test_malformed_stdout_returns_normalized_error():
    backend = FakeBackend(exec_returncode=0, exec_stdout="not json")
    sb = DockerSandbox(pool_size=1, backend=backend)
    result = sb.run_tool(_manifest(), {})
    assert result["ok"] is False
    assert "malformed JSON" in result["error"]


def test_nonzero_returncode_returns_normalized_error():
    backend = FakeBackend(exec_returncode=2, exec_stdout='{"oops": 1}')
    sb = DockerSandbox(pool_size=1, backend=backend)
    result = sb.run_tool(_manifest(), {})
    assert result["ok"] is False
    assert "exited 2" in result["error"]
    assert result["result"] == {"oops": 1}


def test_sync_hooks_invoked_when_provided():
    in_calls, out_calls = [], []
    sb = DockerSandbox(
        pool_size=1, backend=FakeBackend(),
        sync_in_hook=lambda pid, d: in_calls.append((pid, d)),
        sync_out_hook=lambda pid, paths: out_calls.append((pid, paths)),
    )
    sb.sync_in("proj-1", Path("/tmp/x"))
    sb.sync_out("proj-1", ["a", "b"])
    assert in_calls == [("proj-1", Path("/tmp/x"))]
    assert out_calls == [("proj-1", ["a", "b"])]


def test_sync_hooks_default_no_op():
    sb = DockerSandbox(pool_size=1, backend=FakeBackend())
    sb.sync_in("p", Path("/tmp/x"))
    sb.sync_out("p", ["a"])


def test_dispose_stops_all_alive_containers():
    backend = FakeBackend()
    sb = DockerSandbox(pool_size=2, backend=backend)
    sb.run_tool(_manifest(), {})  # starts container-1
    sb.dispose()
    assert backend.stopped == ["container-1"]


def test_invalid_pool_size_rejected():
    with pytest.raises(ValueError):
        DockerSandbox(pool_size=0, backend=FakeBackend())


def test_dockerfile_base_ships_with_package():
    """The package-data glob must include Dockerfile.base, or consumers
    can't `docker build` against an installed wheel."""
    dockerfile = (
        Path(__file__).resolve().parent.parent
        / "src" / "fabri" / "sandbox" / "Dockerfile.base"
    )
    assert dockerfile.exists()
    text = dockerfile.read_text()
    assert "FROM python:3.12-slim" in text
    assert "/opt/fabri" in text


# --- Real-Docker integration: optional, skipped when docker is unavailable.


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_real_docker_backend_round_trip(tmp_path):
    """Smoke test the real CLI plumbing without our test image: spin up
    python:3.12-slim, exec a one-line stdin->stdout tool, verify the result
    flows through DockerBackend correctly. Skips when docker daemon isn't
    reachable (e.g. CI without Docker-in-Docker)."""
    # Cheap reachability check; if the daemon is down we skip instead of
    # failing the test.
    probe = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=5,
    )
    if probe.returncode != 0:
        pytest.skip("docker daemon not reachable")

    backend = DockerBackend()
    cid = backend.run_detached(
        "python:3.12-slim",
        bind_mounts={},
        env={"FABRI_SANDBOX_ROOT": "/workspace"},
    )
    try:
        rc, stdout, _ = backend.exec_tool(
            cid,
            ["python3", "-c", "import sys,json; print(json.dumps({'echo': sys.stdin.read()}))"],
            stdin="hello",
            env={},
            timeout_s=15,
        )
        assert rc == 0
        assert json.loads(stdout) == {"echo": "hello"}
    finally:
        backend.stop(cid)
