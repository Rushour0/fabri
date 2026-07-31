import pytest

from fabri.service.install_store import LinearInstallStore, SlackInstallStore
from fabri.tools.credential_store import (
    CredentialNotFoundError,
    EnvCredentialStore,
    SqliteInstallCredentialStore,
)
from fabri.tools.secret_refs import default_credential_store


def test_slack_default_falls_through_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", "xoxb-default")
    store = SqliteInstallCredentialStore(tmp_path / "installs.db")

    assert store.get("slack", "default") == "xoxb-default"


def test_team_lookup_returns_installed_token(tmp_path):
    db_path = tmp_path / "installs.db"
    SlackInstallStore(db_path).upsert(
        team_id="T123", bot_token="xoxb-installed"
    )

    store = SqliteInstallCredentialStore(db_path)

    assert store.get("slack", "T123") == "xoxb-installed"


def test_unknown_team_fails_closed_without_secret_identifiers(tmp_path):
    db_path = tmp_path / "installs.db"
    token = "xoxb-installed-secret"
    team_id = "T-unknown-sensitive"
    SlackInstallStore(db_path).upsert(team_id="T123", bot_token=token)

    store = SqliteInstallCredentialStore(db_path)

    with pytest.raises(CredentialNotFoundError) as exc_info:
        store.get("slack", team_id)

    message = str(exc_info.value)
    assert team_id not in message
    assert token not in message


def test_missing_database_delegates_to_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_CRED_SLACK_T123", "xoxb-fallback")
    monkeypatch.delenv("FABRI_INSTALL_DB", raising=False)

    unset_store = SqliteInstallCredentialStore()
    absent_store = SqliteInstallCredentialStore(tmp_path / "missing.db")

    assert unset_store.get("slack", "T123") == "xoxb-fallback"
    assert absent_store.get("slack", "T123") == "xoxb-fallback"


def test_linear_default_falls_through_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_CRED_LINEAR_DEFAULT", "lin-default")
    store = SqliteInstallCredentialStore(tmp_path / "installs.db")

    assert store.get("linear", "default") == "lin-default"


def test_linear_workspace_lookup_returns_installed_token(tmp_path):
    db_path = tmp_path / "installs.db"
    LinearInstallStore(db_path).upsert(
        workspace_id="ws-1", access_token="lin-installed"
    )

    store = SqliteInstallCredentialStore(db_path)

    assert store.get("linear", "ws-1") == "lin-installed"


def test_unknown_linear_workspace_fails_closed_without_identifiers(tmp_path):
    db_path = tmp_path / "installs.db"
    token = "lin-installed-secret"
    workspace_id = "ws-unknown-sensitive"
    LinearInstallStore(db_path).upsert(workspace_id="ws-1", access_token=token)

    store = SqliteInstallCredentialStore(db_path)

    with pytest.raises(CredentialNotFoundError) as exc_info:
        store.get("linear", workspace_id)

    message = str(exc_info.value)
    assert workspace_id not in message
    assert token not in message


def test_default_credential_store_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRI_INSTALL_DB", str(tmp_path / "installs.db"))
    assert isinstance(default_credential_store(), SqliteInstallCredentialStore)

    monkeypatch.delenv("FABRI_INSTALL_DB")
    assert isinstance(default_credential_store(), EnvCredentialStore)
