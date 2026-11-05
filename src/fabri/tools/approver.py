"""Approvers: ask a human before a tool marked `always_approve` runs.

`ToolRegistry` calls `approver(name, args, manifest)` for every call to a
tool whose approval class is `always_approve` and runs the call only on a
True answer. With no approver the call is refused. This module supplies two
approvers and picks one for a host:

* `SocketApprover`: the run was started by a host (`fabri serve`, or a
  parent agent) that listens on `FABRI_ASK_USER_SOCKET`. The question goes
  over that socket in the same JSON-line shape `ask_user` uses, with the
  options "approve" and "deny", so Studio shows it as a card with two
  buttons. No reply within the timeout means deny.
* `TtyApprover`: a person is at a terminal. The call is printed on stderr
  and one line is read from stdin. Only "y" or "yes" approves.

`default_approver` picks the socket when its env var is set, else the
terminal when both stdin and stderr are terminals, else nothing (so the
registry refuses the call). No env var ever turns into a standing "yes",
and a child agent, whose stdin is a pipe, cannot approve its own calls.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import uuid
from typing import IO, Mapping

from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import Approver

SOCKET_ENV = "FABRI_ASK_USER_SOCKET"
TIMEOUT_ENV = "FABRI_APPROVAL_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 300.0

APPROVE, DENY = "approve", "deny"
_YES_ANSWERS = frozenset({"approve", "yes", "y"})


def _timeout_from_env(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    raw = env.get(TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
                     "cookie", "credential")
REDACTED = "[redacted]"


def redact_args(value: object) -> object:
    """A copy of `value` where the value of any key whose name contains a
    secret-like word (case ignored) is replaced by "[redacted]", at any
    depth. Message bodies, recipients and other keys stay as they are, since
    the person deciding needs them."""
    if isinstance(value, Mapping):
        return {k: REDACTED if any(p in str(k).lower() for p in _SECRET_KEY_PARTS) else redact_args(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_args(v) for v in value]
    return value


def describe_call(name: str, args: object, manifest: ToolManifest | None, cap: int = 2000) -> str:
    """The text a person reads before deciding: tool name, approval class,
    title when the manifest has one, and the arguments as sorted JSON. Long
    arguments are cut at `cap` characters with a note of how much was left
    out. Secret-looking argument values are redacted first, so the question
    that goes to a trace or a chat surface never holds them."""
    approval = getattr(manifest, "approval", None) or "unknown"
    lines = [f"{name} needs your approval (class {approval})"]
    title = getattr(manifest, "title", None)
    if title:
        lines.append(f"title: {title}")
    lines.append("args:")
    text = json.dumps(redact_args(args), indent=2, sort_keys=True, default=str)
    if len(text) > cap:
        text = f"{text[:cap]}... ({len(text) - cap} more chars)"
    lines.append(text)
    return "\n".join(lines)


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- a closed or odd stream is not a terminal
        return False


class TtyApprover:
    """Ask at the terminal. A lock keeps prompts from parallel tool calls
    from interleaving. Each call checks again that both streams are still
    terminals; if not, or on end of input, the call is denied."""

    def __init__(self, stdin: IO[str] | None = None, stderr: IO[str] | None = None):
        self._stdin = stdin
        self._stderr = stderr
        self._lock = threading.Lock()

    def __call__(self, name: str, args: dict, manifest: ToolManifest) -> bool:
        stdin = self._stdin if self._stdin is not None else sys.stdin
        stderr = self._stderr if self._stderr is not None else sys.stderr
        with self._lock:
            if not (_isatty(stdin) and _isatty(stderr)):
                return False
            stderr.write(describe_call(name, args, manifest) + "\n")
            stderr.write("approve? [y/N] ")
            stderr.flush()
            line = stdin.readline()
            if not line:
                stderr.write("\nno answer; the call was denied\n")
                stderr.flush()
                return False
            return line.strip().lower() in {"y", "yes"}


class SocketApprover:
    """Ask the host listening on a Unix socket. One connection per call, one
    JSON line out, one JSON line back. A timeout denies the call. A refused
    connection, a closed socket or a reply that is not JSON raises
    RuntimeError, which the registry reports as a failed approval check (the
    call is not made). Never reads stdin."""

    def __init__(self, socket_path: str, timeout_s: float | None = None):
        self.socket_path = socket_path
        self.timeout_s = float(timeout_s) if timeout_s is not None else _timeout_from_env()

    def __call__(self, name: str, args: dict, manifest: ToolManifest) -> bool:
        payload = {
            "kind": "approval",
            "question_id": uuid.uuid4().hex,
            "question": describe_call(name, args, manifest),
            "options": [APPROVE, DENY],
            "default": DENY,
        }
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            try:
                sock.connect(self.socket_path)
            except OSError as e:
                raise RuntimeError(f"approval socket unreachable ({type(e).__name__})") from e
            try:
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError as e:
                raise RuntimeError(f"approval socket send failed ({type(e).__name__})") from e
            f = sock.makefile("r", encoding="utf-8")
            try:
                line = f.readline()
            except socket.timeout:
                sys.stderr.write(
                    f"approval: no answer for {name} within {self.timeout_s:.0f}s; "
                    "the call was denied\n"
                )
                sys.stderr.flush()
                return False
            except OSError as e:
                raise RuntimeError(f"approval socket read failed ({type(e).__name__})") from e
            finally:
                f.close()
        finally:
            sock.close()
        if not line:
            raise RuntimeError("approval socket closed before a reply")
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError("approval reply was not JSON") from e
        if not isinstance(reply, dict):
            raise RuntimeError("approval reply was not a JSON object")
        # The ask_user listener echoes question_id in every reply it builds
        # (question_id, answer, and selected_option when a button was used).
        # A reply must carry the id that was sent. A missing or different id
        # is a stale, misrouted or uncorrelated answer, so it denies.
        if reply.get("question_id") != payload["question_id"]:
            sys.stderr.write(
                f"approval: reply for {name} did not carry this question's id; "
                "the call was denied\n"
            )
            sys.stderr.flush()
            return False
        # A selected button is the answer. Free text counts only when no
        # button was selected.
        selected = reply.get("selected_option")
        if selected is not None:
            return selected == APPROVE
        answer = reply.get("answer")
        return isinstance(answer, str) and answer.strip().lower() in _YES_ANSWERS


def default_approver(
    env: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> Approver | None:
    """Pick an approver for this process: the host socket when
    FABRI_ASK_USER_SOCKET is set, else the terminal when stdin and stderr are
    both terminals, else None (the registry then refuses approval-gated
    calls)."""
    env = os.environ if env is None else env
    socket_path = env.get(SOCKET_ENV)
    if socket_path:
        return SocketApprover(socket_path, timeout_s=_timeout_from_env(env))
    stdin_s = stdin if stdin is not None else sys.stdin
    stderr_s = stderr if stderr is not None else sys.stderr
    if _isatty(stdin_s) and _isatty(stderr_s):
        return TtyApprover(stdin=stdin, stderr=stderr)
    return None
