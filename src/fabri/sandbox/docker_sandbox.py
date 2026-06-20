"""S2 -- `DockerSandbox`.

A warm pool of containers built from `Dockerfile.base` (pip install -e
/opt/fabri + the bundled builtins). The pool is per-process; checkout/return
is a simple bounded queue so a host service can run N concurrent agent
turns without paying container-start cost each time.

State ferrying is intentionally *not* baked in. The roadmap says the host
wires the storage backend (MinIO for ludexel, NFS / local volume for
others), so `DockerSandbox` exposes `sync_in_hook` / `sync_out_hook`
callbacks the host can plug. The framework owns the container plumbing;
the host owns the data plumbing.

We do not import `docker` (docker-py) -- shelling out to the `docker` CLI
keeps the framework free of an SDK dependency that would force every
consumer to install it. The CLI is the contract.
"""
from __future__ import annotations

import json
import queue
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from fabri.sandbox import Sandbox
from fabri.tools.manifest_schema import ToolManifest

DEFAULT_IMAGE = "fabri/sandbox:latest"


class DockerBackend:
    """Thin shell over the docker CLI so tests can swap it for a mock.

    Each method maps to one `docker` subcommand. Errors surface as
    `RuntimeError` with the subcommand's stderr -- the sandbox catches
    these and reports `{ok: False}` to the registry, never crashes the
    agent loop.
    """

    def run_detached(self, image: str, *, bind_mounts: dict[str, str], env: dict[str, str]) -> str:
        """Start a container that idle-loops; return its container_id. We
        bind-mount the host's project dir(s) into the container so tool
        subprocesses inside the container see the same paths the host
        agent reasoned about."""
        cmd = ["docker", "run", "-d", "--rm"]
        for src, dst in bind_mounts.items():
            cmd += ["-v", f"{src}:{dst}"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [image, "sleep", "infinity"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def exec_tool(
        self,
        container_id: str,
        command: list[str],
        stdin: str,
        env: dict[str, str],
        timeout_s: float,
    ) -> tuple[int, str, str]:
        """Run the tool's command inside the container with stdin piped in.
        Returns (returncode, stdout, stderr) -- same shape `run_tool`
        already normalizes."""
        cmd = ["docker", "exec", "-i"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [container_id, *command]
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def stop(self, container_id: str) -> None:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=10)


class DockerSandbox(Sandbox):
    """Pooled Docker sandbox.

    `pool_size` is the warm cap; we lazily fill on first checkout rather
    than at __init__ so a host service can construct the sandbox eagerly
    without paying container-start cost up front. `dispose()` is mandatory
    -- a leaked container outlives the agent and silently holds a port /
    bind mount slot.

    `sync_in_hook` and `sync_out_hook` are optional callbacks the host
    wires up. The framework defers state ferrying to them entirely so the
    storage backend stays a host-service concern.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        pool_size: int = 2,
        bind_mounts: dict[str, str] | None = None,
        container_env: dict[str, str] | None = None,
        backend: DockerBackend | None = None,
        sync_in_hook: Callable[[str, Path], None] | None = None,
        sync_out_hook: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self.image = image
        self.pool_size = pool_size
        self.bind_mounts = bind_mounts or {}
        self.container_env = container_env or {}
        self.backend = backend or DockerBackend()
        self.sync_in_hook = sync_in_hook
        self.sync_out_hook = sync_out_hook
        self._pool: queue.Queue[str] = queue.Queue(maxsize=pool_size)
        self._alive: set[str] = set()

    def _acquire(self) -> str:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        if len(self._alive) >= self.pool_size:
            # Pool's at cap but every container is checked out; wait for one
            # to be returned rather than spinning up over the cap.
            return self._pool.get()
        container_id = self.backend.run_detached(
            self.image,
            bind_mounts=self.bind_mounts,
            env=self.container_env,
        )
        self._alive.add(container_id)
        return container_id

    def _release(self, container_id: str) -> None:
        self._pool.put(container_id)

    def run_tool(
        self, manifest: ToolManifest, payload: dict, extra_env: dict | None = None
    ) -> dict:
        container_id = self._acquire()
        env = dict(self.container_env)
        if extra_env:
            env.update(extra_env)
        try:
            returncode, stdout, stderr = self.backend.exec_tool(
                container_id,
                manifest.command,
                stdin=json.dumps(payload),
                env=env,
                timeout_s=manifest.timeout_s,
            )
        except subprocess.TimeoutExpired:
            # On timeout we destroy the container -- we don't know what
            # state the tool left it in. The pool will lazily refill on the
            # next acquire.
            self.backend.stop(container_id)
            self._alive.discard(container_id)
            return {"ok": False, "error": f"timeout after {manifest.timeout_s}s"}
        except Exception as e:
            return {"ok": False, "error": f"docker exec failed: {e}"}
        finally:
            if container_id in self._alive:
                self._release(container_id)

        out: dict = {"stderr": stderr} if stderr else {}
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "malformed JSON output from tool", **out}
        if returncode != 0:
            return {"ok": False, "error": f"tool exited {returncode}", "result": parsed, **out}
        return {"ok": True, "result": parsed, **out}

    def sync_in(self, project_id: str, target_dir: Path) -> None:
        if self.sync_in_hook is not None:
            self.sync_in_hook(project_id, target_dir)

    def sync_out(self, project_id: str, dirty_paths: list[str]) -> None:
        if self.sync_out_hook is not None:
            self.sync_out_hook(project_id, dirty_paths)

    def dispose(self) -> None:
        # Drain pool + kill anything still alive. We don't track checked-out
        # containers separately -- the host calling dispose() during an
        # active run is a bug, not something we need to guard against.
        for container_id in list(self._alive):
            try:
                self.backend.stop(container_id)
            except Exception:
                pass
        self._alive.clear()
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except queue.Empty:
                break
