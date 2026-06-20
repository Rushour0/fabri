"""S1 -- `fabri.sandbox` package.

The `Sandbox` ABC is the seam between fabri-the-framework and the host's
isolation model. Today the only isolation is a cwd-style `$FABRI_SANDBOX_ROOT`
env var that every file/shell tool checks (read_file.py, write_file.py,
python_exec.py, bash.py, edit_file.py). `LocalSandbox` keeps that exact
behavior -- a registry built without an explicit sandbox falls back to it,
so existing configs and tests stay green.

`DockerSandbox` (S2) plugs into the same ABC: a warm pool of containers,
`sync_in/sync_out` ferry project state via the host service's storage
backend (MinIO / S3 / NFS / local volume). The framework ships the
interface; consuming services wire the backend.

Why an ABC at all (instead of just a function): `sync_in/sync_out` and
`dispose` only make sense once you have a multi-instance backend. Locking
those into a `run_tool` signature would force LocalSandbox to grow no-op
methods anyway, and consumers (ludexel-service) need to type-check against
"is this a Docker-style sandbox we can pool?" The ABC is that type.
"""
from abc import ABC, abstractmethod
from pathlib import Path

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.runner import run_tool


class Sandbox(ABC):
    """Isolation contract every fabri tool invocation routes through.

    `run_tool` is the only method tools depend on; everything else exists
    so the host service can checkout/return + ferry project state without
    coupling to a specific backend.
    """

    @abstractmethod
    def run_tool(
        self, manifest: ToolManifest, payload: dict, extra_env: dict | None = None
    ) -> dict:
        """Invoke the tool's subprocess inside this sandbox. Return shape
        matches `fabri.tools.runner.run_tool`: {ok, error?, result?, stderr?}."""

    def sync_in(self, project_id: str, target_dir: Path) -> None:
        """Pull the project's state into `target_dir`. No-op for backends
        that don't ferry state (LocalSandbox)."""
        return None

    def sync_out(self, project_id: str, dirty_paths: list[str]) -> None:
        """Push the listed paths back to the host's storage backend. No-op
        for backends that don't ferry state."""
        return None

    def dispose(self) -> None:
        """Release any resources. No-op for stateless backends."""
        return None


__all__ = ["Sandbox", "LocalSandbox", "DockerSandbox"]


class LocalSandbox(Sandbox):
    """Today's behavior, lifted unchanged into an object.

    Tools run as ordinary subprocesses inheriting the framework process's env
    plus an extra `FABRI_SANDBOX_ROOT` set by the registry. Each file/shell
    tool independently enforces the path jail against that env var
    (read_file.py:11, write_file.py:11, ...). No ferrying -- the project
    state already lives on the local disk the runner sees.
    """

    def run_tool(
        self, manifest: ToolManifest, payload: dict, extra_env: dict | None = None
    ) -> dict:
        return run_tool(manifest, payload, extra_env=extra_env)


def __getattr__(name):
    # Lazy import so `from fabri.sandbox import Sandbox, LocalSandbox` never
    # pulls in the Docker module unless the consumer asks for it (avoids any
    # one-time import cost of the subprocess/queue plumbing for the common
    # LocalSandbox case).
    if name == "DockerSandbox":
        from fabri.sandbox.docker_sandbox import DockerSandbox

        return DockerSandbox
    raise AttributeError(f"module 'fabri.sandbox' has no attribute {name!r}")
