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
import re
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
                     "cookie", "credential", "bearer")
REDACTED = "[redacted]"

# Keys that label a sibling value instead of holding the secret themselves, as
# in {"name": "Authorization", "value": "Bearer x"} or {"type": "bearer",
# "value": "..."}. When a label names a secret word, every other entry in that
# object is redacted. The labels stay visible so the person can see what was
# hidden.
_LABEL_KEYS = frozenset({"name", "key", "header", "field", "type"})

# A whole string that is an auth header value: the scheme plus one token.
_AUTH_HEADER_VALUE = re.compile(r"(?i)^\s*(bearer|basic)\s+(\S+)\s*$")

# Common token shapes, matched anywhere inside a string. Only the token is
# replaced, so a message body that quotes one stays readable.
_TOKEN_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|gh[po]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|xox[bp]-[A-Za-z0-9-]{8,}"
    r"|ck_pat_[A-Za-z0-9_-]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r")"
)


def _names_secret(name: object) -> bool:
    """True when a key or label names a secret word. Dashes and spaces count
    as underscores, so "X-Api-Key" matches "api_key"."""
    text = str(name).lower().replace("-", "_").replace(" ", "_")
    return any(part in text for part in _SECRET_KEY_PARTS)


def _looks_like_basic_credential(token: str) -> bool:
    """Basic auth carries base64, which a plain word like "question" is not.
    Mixed case after the first letter, a digit, or a base64 symbol counts."""
    if not re.fullmatch(r"[A-Za-z0-9+/]{6,}={0,2}", token):
        return False
    rest = token[1:]
    return (any(c.isdigit() or c in "+/=" for c in token)
            or (any(c.isupper() for c in rest) and any(c.islower() for c in rest)))


def _redact_string(text: str) -> str:
    match = _AUTH_HEADER_VALUE.match(text)
    if match:
        scheme, token = match.group(1).lower(), match.group(2)
        if scheme == "bearer" or _looks_like_basic_credential(token):
            return REDACTED
    return _TOKEN_SHAPES.sub(REDACTED, text)


def redact_args(value: object) -> object:
    """A copy of `value` with secrets replaced by "[redacted]", at any depth.

    Three rules apply. The value of any key whose name contains a secret-like
    word (case ignored) is redacted. In an object whose name, key, header,
    field or type entry names a secret word, the other entries are redacted.
    A string that is a Bearer or Basic header value is redacted whole, and
    common token shapes (sk-, ghp_, xoxb-, AKIA, JWTs and so on) are cut out
    of any string. Recipients, subjects, message bodies and other fields stay
    readable, since the person deciding needs them."""
    if isinstance(value, Mapping):
        labelled = any(
            str(k).lower() in _LABEL_KEYS and isinstance(v, str) and _names_secret(v)
            for k, v in value.items()
        )
        out = {}
        for k, v in value.items():
            if _names_secret(k) or (labelled and str(k).lower() not in _LABEL_KEYS):
                out[k] = REDACTED
            else:
                out[k] = redact_args(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_args(v) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
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
