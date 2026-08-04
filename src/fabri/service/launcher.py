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

# --- child environment allowlist ---------------------------------------------
#
# A run is a subprocess that executes model-chosen tool calls, and several
# bundled agencies enable `bash`. Handing it `{**os.environ}` therefore hands
# every service secret to whatever the model decides to run: the Slack signing
# secret and bot tokens, the GitHub App private key, the Linear client secret,
# the session-cookie secret, and the path to installs.db (which holds every
# connected tenant's token in plaintext). None of those belong to an agent --
# the *service* talks to Slack/GitHub/Linear, the agent never does.
#
# So the child env is built from an allowlist, not filtered by a denylist: a
# denylist silently leaks every variable nobody thought of, and new secrets get
# added to deployments far more often than this file gets edited.
#
# What stays: the interpreter's own runtime, TLS trust, proxies, model/embedding
# caches, the vector store the memory layer needs, provider credentials (an
# agent cannot call an LLM without one), and the FABRI_* knobs that shape the
# run itself. Operators who need more can name it -- see FABRI_RUN_ENV_ALLOW.

_ALLOWED_EXACT = frozenset({
    # interpreter + process basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "TEMP", "TMP",
    "TZ", "TERM", "LANG",
    "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
    "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT",
    # TLS trust + proxies, or every outbound call fails behind a corporate CA
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    # model + embedding caches: without these every run re-downloads MiniLM
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
    "SENTENCE_TRANSFORMERS_HOME", "TOKENIZERS_PARALLELISM", "XDG_CACHE_HOME",
    # the retrieval backend
    "QDRANT_URL", "QDRANT_API_KEY",
    # provider credentials -- the run's whole reason to exist
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    # run-shaping FABRI_* knobs (see the deny note below for the rest)
    "FABRI_HOME", "FABRI_SESSION_ID",
    "FABRI_ASK_USER_SOCKET", "FABRI_ASK_USER_TIMEOUT_S",
    "FABRI_SANDBOX_ROOT", "FABRI_SANDBOX_ROOT_OVERRIDE",
    "FABRI_SUBAGENT_DEPTH", "FABRI_SUBAGENT_MAX_DEPTH",
    "FABRI_DISABLE_SUBAGENT_MINING", "FABRI_EPHEMERAL",
    "FABRI_BLOCK_START", "FABRI_BLOCK_END",
    "FABRI_FETCH_ALLOW_PRIVATE", "FABRI_REPO_TEST_CMD",
    "FABRI_OTLP_ENDPOINT", "FABRI_OTLP_PROTOCOL", "FABRI_OTLP_INSECURE",
    "FABRI_OTLP_HEADERS",
})

_ALLOWED_PREFIXES = ("LC_",)

# Deliberately absent, and worth naming so nobody re-adds them by reflex:
#   FABRI_AUTH_SECRET / FABRI_ADMIN_TOKEN  -- forge a session or an admin call
#   FABRI_INSTALL_DB                       -- every tenant's token, plaintext
#   FABRI_CRED_*                           -- connector credentials
#   FABRI_SLACK_* / SLACK_*                -- bot token, signing secret
#   GITHUB_APP_*                           -- App id + private key (inline PEM)
#   LINEAR_CLIENT_*                        -- OAuth client secret
#   FABRI_CONFIG                           -- would redirect the run's own config
# An agency that genuinely needs one (a self-hosted crew posting to its own
# Slack) re-admits it explicitly via FABRI_RUN_ENV_ALLOW.

_ALLOW_ENV_VAR = "FABRI_RUN_ENV_ALLOW"


def _operator_allowlist(base_env: dict) -> tuple[str, ...]:
    """Extra names an operator re-admitted via ``FABRI_RUN_ENV_ALLOW``.

    Comma-separated; a trailing ``*`` makes it a prefix
    (``FABRI_RUN_ENV_ALLOW=FABRI_CRED_*,MY_TOKEN``).
    """
    raw = base_env.get(_ALLOW_ENV_VAR) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def build_child_env(
    base_env: dict,
    *,
    allow: Sequence[str] = (),
    scrub: bool = True,
) -> dict:
    """The environment one run may see: an allowlist copy of ``base_env``.

    ``allow`` names extra variables the caller knows this run needs -- the
    service passes the ``api_key_env`` values it finds in the bound config, so a
    crew pointing at a provider key this module has never heard of still works.
    Names ending in ``*`` are prefix matches.

    ``scrub=False`` restores the old inherit-everything behavior for callers
    that are not exposing runs to untrusted input.
    """
    if not scrub:
        return {**base_env}

    patterns = tuple(allow) + _operator_allowlist(base_env)
    exact = _ALLOWED_EXACT | {p for p in patterns if not p.endswith("*")}
    prefixes = _ALLOWED_PREFIXES + tuple(p[:-1] for p in patterns if p.endswith("*"))

    return {
        key: value
        for key, value in base_env.items()
        if key in exact or key.startswith(prefixes)
    }


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

    ``--config`` is a *global* option on the ``fabri`` parser, so it must appear
    BEFORE the ``run`` subcommand (``fabri --config X run <task>``); placing it
    after ``run`` makes argparse reject it with exit code 2.
    """
    return [
        python_exe or sys.executable,
        "-m",
        "fabri.cli",
        "--config",
        str(config_path),
        "run",
        task,
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
            # `fabri run` prints the result envelope FIRST, then may emit a
            # human-readable trailer (e.g. a memory-synthesis note). Decode the
            # first complete JSON object off the top rather than requiring the
            # whole stream to be JSON.
            try:
                obj, _ = json.JSONDecoder().raw_decode(out.lstrip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            # Fallback: the last JSON object line, else surface the raw tail.
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
    allow_env: Sequence[str] = (),
    scrub_env: bool = True,
) -> RunHandle:
    """Spawn one agent run and return a :class:`RunHandle`.

    ``fabri_home`` becomes the child's ``FABRI_HOME`` (its trace/log/locks root).
    ``command`` overrides the default ``fabri run`` argv -- tests pass a fake
    script here; production leaves it ``None``. The child always inherits
    ``FABRI_HOME`` and ``FABRI_SESSION_ID`` in its env so an alternate command
    can resolve the same trace path the launcher will tail.

    The child's env is an allowlist copy of the service's (see
    :func:`build_child_env`): service secrets never reach a run. ``allow_env``
    adds names this particular run needs, and ``env`` entries are applied last,
    so an explicit value always wins over the allowlist.
    """
    session_id = session_id or str(uuid.uuid4())
    home = Path(fabri_home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    trace = run_trace_path(home, session_id)
    trace.parent.mkdir(parents=True, exist_ok=True)

    child_env = build_child_env(dict(os.environ), allow=allow_env, scrub=scrub_env)
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
