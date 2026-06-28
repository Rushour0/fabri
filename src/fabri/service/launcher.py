"""B7 -- run launcher: spawn the agent as a fresh subprocess.

The service never re-implements the agent loop; it shells out to the same
``fabri run`` CLI a human would. Each run gets a fresh ``session_id`` and its
own ``FABRI_HOME`` (so concurrent tenants never share a traces/locks dir), is
started with ``start_new_session=True`` (its own process group, so killing the
service doesn't orphan-signal a long child mid-write), and writes its trace to
``$FABRI_HOME/.fabri/traces/<session_id>.jsonl`` -- which :mod:`.tailer` follows.

``build_run_command`` is exposed as a pure function so the argv plumbing is
unit-testable in isolation, mirroring ``tools/examples/spawn_subagent.py``'s
``build_runner_command``. Integration tests stub the agent by passing a
``command`` that points at a tiny fake script (same pattern the spawn_subagent
tests use), which reads ``FABRI_HOME`` / ``FABRI_SESSION_ID`` from the env, writes
a known trace, and prints a result envelope.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fabri.service.tailer import run_trace_path


def build_run_command(
    task: str,
    config_path: str | Path,
    session_id: str,
    *,
    python_exe: str | None = None,
) -> list[str]:
    """The argv for one agent run: ``python -m fabri.cli run <task> ...``.

    ``--session-id`` is passed so the launcher (not the agent) owns the id and
    can resolve the trace path before the child starts writing.
    """
    return [
        python_exe or sys.executable,
        "-m",
        "fabri.cli",
        "run",
        task,
        "--config",
        str(config_path),
        "--session-id",
        session_id,
    ]


@dataclass
class RunHandle:
    """A live (or finished) agent run: its id, home, trace path, and process.

    ``is_running`` is what :func:`fabri.service.tailer.tail_events` polls;
    ``result`` blocks for the process and parses its stdout envelope (the same
    JSON ``fabri run`` prints).
    """

    session_id: str
    fabri_home: Path
    trace_path: Path
    proc: subprocess.Popen
    _stdout: str | None = field(default=None, repr=False)
    _stderr: str | None = field(default=None, repr=False)

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        out, err = self.proc.communicate(timeout=timeout)
        if self._stdout is None:
            self._stdout = out or ""
        if self._stderr is None:
            self._stderr = err or ""
        return self.proc.returncode

    def result(self, timeout: float | None = None) -> dict:
        """Block for the run, returning its result envelope.

        Parses the agent's stdout JSON (``session_id``, ``success``, ``outcome``,
        ``final_text``, ``structured_output``, ``usage``). If stdout isn't JSON
        (the child crashed before printing), returns an error envelope carrying
        the return code + a stderr tail so a host can surface the failure.
        """
        returncode = self.wait(timeout=timeout)
        out = (self._stdout or "").strip()
        if not out:
            return {
                "session_id": self.session_id,
                "success": False,
                "error": f"agent exited {returncode} with no stdout",
                "returncode": returncode,
                "stderr_tail": (self._stderr or "")[-2000:],
            }
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            # Some launchers (and the CLI) may print a non-JSON trailer; take the
            # last JSON object line if present, else surface the raw tail.
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return {
                "session_id": self.session_id,
                "success": False,
                "error": "agent stdout was not JSON",
                "returncode": returncode,
                "stdout_tail": out[-2000:],
                "stderr_tail": (self._stderr or "")[-2000:],
            }

    def terminate(self) -> None:
        if self.is_running():
            self.proc.terminate()


def launch_run(
    task: str,
    *,
    config_path: str | Path,
    fabri_home: str | Path,
    session_id: str | None = None,
    command: Sequence[str] | None = None,
    env: dict | None = None,
) -> RunHandle:
    """Spawn one agent run and return a :class:`RunHandle`.

    ``fabri_home`` becomes the child's ``FABRI_HOME`` (its trace/log/locks root).
    ``command`` overrides the default ``fabri run`` argv -- tests pass a fake
    script here; production leaves it ``None``. The child always inherits
    ``FABRI_HOME`` and ``FABRI_SESSION_ID`` in its env so an alternate command
    can resolve the same trace path the launcher will tail.
    """
    session_id = session_id or str(uuid.uuid4())
    home = Path(fabri_home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    trace = run_trace_path(home, session_id)
    trace.parent.mkdir(parents=True, exist_ok=True)

    child_env = {**os.environ}
    child_env["FABRI_HOME"] = str(home)
    child_env["FABRI_SESSION_ID"] = session_id
    if env:
        child_env.update({k: str(v) for k, v in env.items()})

    argv = list(command) if command is not None else build_run_command(
        task, config_path, session_id
    )
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
        start_new_session=True,
    )
    return RunHandle(
        session_id=session_id,
        fabri_home=home,
        trace_path=trace,
        proc=proc,
    )
