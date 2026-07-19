"""GitHub issue helpers for fabri runs."""
from __future__ import annotations

from .github import (
    GitHubError,
    comment_issue,
    create_issue,
    find_open_issue_with_marker,
    list_open_issues_with_label,
)
from .base import RepoProvider, detect_provider, get_provider


def open_or_update_tracking_issue(
    repo: str,
    token: str,
    title: str,
    body: str,
    key: str,
    labels: list[str] | None = None,
) -> str:
    """Create a marked tracking issue, or append a marked update to it."""
    # Kept as the original GitHub-compatible public helper.  New CLI code uses
    # the provider interface directly, while callers of this helper retain its
    # exact historical request shape (notably `labels=None`).
    marker = f"<!-- fabri:repo:{key} -->"
    marked_body = f"{body.rstrip()}\n\n{marker}"
    existing = find_open_issue_with_marker(
        repo, token, marker, label=labels[0] if labels else None
    )
    if existing is not None:
        comment_issue(repo, token, existing["number"], f"Update:\n\n{marked_body}")
        return existing["html_url"]
    return create_issue(repo, token, title, marked_body, labels=labels)


__all__ = [
    "GitHubError",
    "comment_issue",
    "create_issue",
    "find_open_issue_with_marker",
    "list_open_issues_with_label",
    "open_or_update_tracking_issue",
    "RepoProvider",
    "detect_provider",
    "get_provider",
]
