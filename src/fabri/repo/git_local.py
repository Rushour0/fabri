"""Local git operations used to commit and push checkout changes."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Collection, Mapping
from urllib.parse import unquote, urlsplit


_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s]+@")


def _redact(text: str, remote_url: str) -> str:
    """Remove credentials from git output before it reaches an exception."""
    redacted = _URL_CREDENTIALS.sub(r"\1***@", text)

    parsed = urlsplit(remote_url)
    credentials: set[str] = set()
    if parsed.username:
        credentials.update((parsed.username, unquote(parsed.username)))
    if parsed.password:
        credentials.update((parsed.password, unquote(parsed.password)))

    for credential in sorted(credentials, key=len, reverse=True):
        if credential:
            redacted = redacted.replace(credential, "***")

    return redacted


def _run_git(
    checkout_dir: str | os.PathLike[str],
    *args: str,
    operation: str,
    remote_url: str = "",
    env: Mapping[str, str] | None = None,
    allowed_returncodes: Collection[int] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", os.fspath(checkout_dir), *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        detail = _redact(str(exc), remote_url)
        raise RuntimeError(f"git {operation} could not run: {detail}") from exc

    if completed.returncode not in allowed_returncodes:
        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        detail = _redact(output, remote_url) or "no output"
        raise RuntimeError(
            f"git {operation} failed with exit code "
            f"{completed.returncode}: {detail}"
        )

    return completed


def has_changes(checkout_dir: str | os.PathLike[str]) -> bool:
    """Return whether a checkout has staged, unstaged, or untracked changes."""
    completed = _run_git(
        checkout_dir,
        "status",
        "--porcelain",
        operation="status",
    )
    return bool(completed.stdout.strip())


def commit_and_push_all(
    checkout_dir: str | os.PathLike[str],
    *,
    base: str,
    new_branch: str,
    commit_msg: str,
    remote_url: str,
    author_name: str = "fabri",
    author_email: str = "fabri@localhost",
) -> str | None:
    """Commit all checkout changes and push current HEAD to ``new_branch``.

    The caller prepares the checkout at ``base``. The commit remains on the
    current local branch and is pushed directly to the requested remote ref.
    """
    _ = base

    _run_git(
        checkout_dir,
        "add",
        "-A",
        operation="add",
        remote_url=remote_url,
    )

    staged_diff = _run_git(
        checkout_dir,
        "diff",
        "--cached",
        "--quiet",
        "--exit-code",
        operation="diff",
        remote_url=remote_url,
        allowed_returncodes=(0, 1),
    )
    if staged_diff.returncode == 0:
        return None

    identity_env = os.environ.copy()
    identity_env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    _run_git(
        checkout_dir,
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "-m",
        commit_msg,
        operation="commit",
        remote_url=remote_url,
        env=identity_env,
    )

    _run_git(
        checkout_dir,
        "push",
        "--force",
        remote_url,
        f"HEAD:refs/heads/{new_branch}",
        operation="push",
        remote_url=remote_url,
    )
    return new_branch
