"""Provider-neutral helpers for filing fabri repository work."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import Protocol
from urllib.parse import quote


class RepoProvider(Protocol):
    """The deliberately small surface shared by supported code hosts."""

    def open_or_update_issue(
        self, repo: str, title: str, body: str, key: str, labels: list[str]
    ) -> str: ...

    def open_or_update_pr(
        self, repo: str, title: str, body: str, key: str, head: str, base: str
    ) -> str: ...

    def push_branch(
        self, repo: str, base: str, new_branch: str, file_path: str,
        file_text: str, commit_msg: str,
    ) -> None: ...


def detect_provider(repo: str | None, host_env: dict[str, str] | None = None) -> str:
    """Infer a provider from CI environment, falling back to GitHub."""
    env = host_env if host_env is not None else os.environ
    if env.get("GITLAB_CI") or env.get("CI_PROJECT_PATH") or env.get("GITLAB_PROJECT_ID"):
        return "gitlab"
    if env.get("BITBUCKET_BUILD_NUMBER") or env.get("BITBUCKET_REPO_FULL_NAME"):
        return "bitbucket"
    # GITHUB_REPOSITORY deliberately wins only after the more specific hosts.
    if env.get("GITHUB_REPOSITORY") or repo:
        return "github"
    return "github"


def get_provider(name: str, token: str) -> RepoProvider:
    """Return the requested stdlib-only provider adapter."""
    if name == "github":
        from .github import GitHubProvider
        return GitHubProvider(token)
    if name == "gitlab":
        from .gitlab import GitLabProvider
        return GitLabProvider(token)
    if name == "bitbucket":
        from .bitbucket import BitbucketProvider
        return BitbucketProvider(token)
    raise ValueError(f"unsupported repository provider: {name}")


def push_branch_with_url(
    remote_url: str, base: str, new_branch: str, file_path: str,
    file_text: str, commit_msg: str,
) -> None:
    """Make a one-file commit in an isolated clone and push it."""
    relative_path = Path(file_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("file_path must be relative to the repository root")
    with tempfile.TemporaryDirectory(prefix="fabri-repo-") as temporary_dir:
        clone_dir = Path(temporary_dir) / "repo"
        subprocess.run(["git", "clone", "--branch", base, remote_url, str(clone_dir)], check=True)
        subprocess.run(["git", "-C", str(clone_dir), "checkout", "-B", new_branch, f"origin/{base}"], check=True)
        target = clone_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text)
        subprocess.run(["git", "-C", str(clone_dir), "add", "--", file_path], check=True)
        subprocess.run(["git", "-C", str(clone_dir), "-c", "user.name=fabri", "-c", "user.email=fabri@localhost", "commit", "-m", commit_msg], check=True)
        subprocess.run(["git", "-C", str(clone_dir), "push", "--force-with-lease", "-u", "origin", new_branch], check=True)


def token_url(prefix: str, token: str, host: str, repo: str) -> str:
    """Build an HTTPS remote while escaping tokens that contain URL syntax."""
    return f"https://{prefix}:{quote(token, safe='')}@{host}/{repo}.git"
