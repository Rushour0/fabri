from __future__ import annotations

import subprocess
from pathlib import Path

from fabri.repo.git_local import commit_and_push_all, has_changes


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _setup_repositories(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    origin = tmp_path / "origin.git"

    _git("init", str(checkout))
    _git("-C", str(checkout), "config", "user.name", "Fixture User")
    _git("-C", str(checkout), "config", "user.email", "fixture@example.com")
    _git("init", "--bare", str(origin))

    return checkout, origin


def _revision(repository: Path, ref: str) -> str:
    return _git(
        f"--git-dir={repository}",
        "rev-parse",
        "--verify",
        ref,
    ).stdout.strip()


def test_commit_and_push_all_commits_two_files(tmp_path: Path) -> None:
    checkout, origin = _setup_repositories(tmp_path)
    (checkout / "first.txt").write_text("first\n", encoding="utf-8")
    (checkout / "second.txt").write_text("second\n", encoding="utf-8")

    branch = "fabri/two-files"
    result = commit_and_push_all(
        checkout,
        base="main",
        new_branch=branch,
        commit_msg="Add two files",
        remote_url=str(origin),
        author_name="Fabri Test",
        author_email="fabri-test@example.com",
    )

    assert result == branch
    remote_revision = _revision(origin, f"refs/heads/{branch}")
    local_revision = _git("-C", str(checkout), "rev-parse", "HEAD").stdout.strip()
    assert remote_revision == local_revision
    identity = _git(
        "-C",
        str(checkout),
        "show",
        "-s",
        "--format=%an <%ae>|%cn <%ce>",
        "HEAD",
    ).stdout.strip()
    assert identity == (
        "Fabri Test <fabri-test@example.com>|"
        "Fabri Test <fabri-test@example.com>"
    )


def test_commit_and_push_all_returns_none_for_empty_diff(tmp_path: Path) -> None:
    checkout, origin = _setup_repositories(tmp_path)
    (checkout / "tracked.txt").write_text("content\n", encoding="utf-8")
    branch = "fabri/empty-diff"

    commit_and_push_all(
        checkout,
        base="main",
        new_branch=branch,
        commit_msg="Initial commit",
        remote_url=str(origin),
    )
    commit_before = _git("-C", str(checkout), "rev-parse", "HEAD").stdout.strip()

    result = commit_and_push_all(
        checkout,
        base="main",
        new_branch=branch,
        commit_msg="Must not be created",
        remote_url=str(origin),
    )

    commit_after = _git("-C", str(checkout), "rev-parse", "HEAD").stdout.strip()
    assert result is None
    assert commit_after == commit_before
    assert _revision(origin, f"refs/heads/{branch}") == commit_before


def test_commit_and_push_all_updates_same_branch(tmp_path: Path) -> None:
    checkout, origin = _setup_repositories(tmp_path)
    tracked = checkout / "tracked.txt"
    tracked.write_text("version one\n", encoding="utf-8")
    branch = "fabri/idempotent"

    assert (
        commit_and_push_all(
            checkout,
            base="main",
            new_branch=branch,
            commit_msg="First version",
            remote_url=str(origin),
        )
        == branch
    )
    first_revision = _revision(origin, f"refs/heads/{branch}")

    tracked.write_text("version two\n", encoding="utf-8")
    assert (
        commit_and_push_all(
            checkout,
            base="main",
            new_branch=branch,
            commit_msg="Second version",
            remote_url=str(origin),
        )
        == branch
    )
    second_revision = _revision(origin, f"refs/heads/{branch}")

    assert second_revision != first_revision
    assert second_revision == _git(
        "-C",
        str(checkout),
        "rev-parse",
        "HEAD",
    ).stdout.strip()


def test_has_changes_for_untracked_staged_modified_and_clean_tree(
    tmp_path: Path,
) -> None:
    checkout, _ = _setup_repositories(tmp_path)
    tracked = checkout / "tracked.txt"

    assert has_changes(checkout) is False

    tracked.write_text("initial\n", encoding="utf-8")
    assert has_changes(checkout) is True

    _git("-C", str(checkout), "add", "tracked.txt")
    assert has_changes(checkout) is True

    _git("-C", str(checkout), "commit", "-m", "Fixture commit")
    assert has_changes(checkout) is False

    tracked.write_text("modified\n", encoding="utf-8")
    assert has_changes(checkout) is True
