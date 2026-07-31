"""Unit tests for GitHub App installation storage."""
from __future__ import annotations

import pytest

from fabri.service.install_store import GitHubInstallStore


pytestmark = pytest.mark.unit


def test_upsert_preserves_installed_at_and_advances_updated_at(
    tmp_path,
) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")

    store.upsert(
        installation_id="456",
        account_login="acme",
        repos=["acme/app"],
        now=1000.0,
    )
    store.upsert(
        installation_id="456",
        account_login="acme-renamed",
        repos=["acme-renamed/app"],
        now=2000.0,
    )

    install = store.get("456")
    assert install is not None
    assert install["installed_at"] == 1000.0
    assert install["updated_at"] == 2000.0
    assert install["account_login"] == "acme-renamed"


def test_upsert_coalesces_partial_writes_in_both_orders(tmp_path) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")

    store.upsert(installation_id="first")
    store.upsert(
        installation_id="first",
        account_login="acme",
        account_type="Organization",
        repos=["acme/app"],
    )
    first = store.get("first")
    assert first is not None
    assert first["account_login"] == "acme"
    assert first["account_type"] == "Organization"
    assert first["repos"] == ["acme/app"]

    store.upsert(
        installation_id="second",
        account_login="octo",
        account_type="User",
        repos=["octo/project"],
    )
    store.upsert(
        installation_id="second",
        account_login=None,
        account_type=None,
        repos=None,
    )
    second = store.get("second")
    assert second is not None
    assert second["account_login"] == "octo"
    assert second["account_type"] == "User"
    assert second["repos"] == ["octo/project"]


def test_get_and_list_are_token_free_and_decode_repos(tmp_path) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")
    store.upsert(
        installation_id="with-repos",
        repos=["acme/app"],
    )
    store.upsert(installation_id="without-repos")

    install = store.get("with-repos")
    assert install is not None
    assert install["repos"] == ["acme/app"]
    assert isinstance(install["repos"], list)

    installs = store.list()
    assert {row["installation_id"] for row in installs} == {
        "with-repos",
        "without-repos",
    }
    assert all(row["repos"] is None or isinstance(row["repos"], list) for row in installs)

    forbidden_keys = {"token", "access_token", "bot_token"}
    assert forbidden_keys.isdisjoint(install)
    assert all(forbidden_keys.isdisjoint(row) for row in installs)


def test_get_installation_matches_account_and_repo_owner(tmp_path) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")
    store.upsert(
        installation_id="456",
        account_login="acme",
        repos=["elsewhere/project"],
    )

    assert store.get_installation("acme") == "456"
    assert store.get_installation("acme/whatever") == "456"


def test_get_installation_matches_exact_repo(tmp_path) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")
    store.upsert(
        installation_id="456",
        account_login="org",
        repos=["acme/app-1", "acme/app-2"],
    )

    assert store.get_installation("acme/app-1") == "456"


def test_get_installation_does_not_use_like_or_substring_matching(
    tmp_path,
) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")
    store.upsert(
        installation_id="456",
        account_login="different-owner",
        repos=["acme/app-2"],
    )

    assert store.get_installation("org") is None
    assert store.get_installation("acme/a-2") is None
    assert store.get_installation("unknown") is None


def test_delete_reports_whether_installation_existed(tmp_path) -> None:
    store = GitHubInstallStore(tmp_path / "installs.db")
    store.upsert(installation_id="456")

    assert store.delete("456") is True
    assert store.delete("456") is False

