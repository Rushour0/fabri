"""PR #100 review: a connection stored with `fabri connect` (into
$FABRI_HOME/serve/installs.db) resolves in a direct `fabri run`, while a run
`fabri serve` launched never opens the install database."""
from __future__ import annotations

import argparse
import os
import sqlite3

import pytest

from fabri import cli
from fabri.tools import secret_refs
from fabri.tools.credential_broker_client import BROKER_ENV_NAMES
from fabri.tools.credential_store import CredentialNotFoundError
from fabri.tools.secret_refs import resolve_secret

TOKEN = "lin_api_stored_token_5d1e9a"


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("FABRI_INSTALL_DB", "FABRI_SESSION_ID", "FABRI_RUN_ACTOR", "FABRI_CRED_AUDIT_LOG",
                 "FABRI_CRED_LINEAR_ACME", *BROKER_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    monkeypatch.setattr(secret_refs, "_cli_install_db", None)
    return tmp_path


def _connect(home) -> None:
    """Write the row the way `fabri connect` does: through its own store
    opener, with no --home-root and no FABRI_INSTALL_DB."""
    store = cli._connection_store(argparse.Namespace(home_root=None), create=True)
    store.upsert(provider="linear", handle="acme", access_token=TOKEN)
    assert (home / "serve" / "installs.db").is_file()


def _run_args(**kw) -> argparse.Namespace:
    return argparse.Namespace(config="unused.yaml", task="t", dry_run=True, **kw)


def _dry_run_resolving(monkeypatch, seen: dict):
    """cmd_run with the config load stubbed; the dry-run step resolves the
    ref the way a tool source does (default store, no store passed)."""
    monkeypatch.setattr(cli, "load_config", lambda path: {})

    def fake_dry_run(config, task):
        try:
            seen["value"] = resolve_secret("linear:acme")
        except CredentialNotFoundError as exc:
            seen["error"] = str(exc)

    monkeypatch.setattr(cli, "_print_dry_run", fake_dry_run)


def test_connect_then_run_resolves_the_stored_connection(home, monkeypatch):
    _connect(home)
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert seen == {"value": TOKEN}
    # The path is never exported, so tool subprocesses cannot inherit it.
    assert "FABRI_INSTALL_DB" not in os.environ


def test_without_the_cli_hook_the_ref_is_missing(home):
    # The bug the fix closes: the default store alone does not see the db.
    _connect(home)
    with pytest.raises(CredentialNotFoundError):
        resolve_secret("linear:acme")


def test_missing_db_falls_back_to_env(home, monkeypatch):
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    monkeypatch.setenv("FABRI_CRED_LINEAR_ACME", "from-env-value")
    cli.cmd_run(_run_args())
    assert seen == {"value": "from-env-value"}
    assert secret_refs._cli_install_db is None


def _forbid_db(monkeypatch, home):
    db = str(home / "serve" / "installs.db")
    real = sqlite3.connect

    def guarded(path, *a, **kw):
        if str(path) == db:
            raise AssertionError("service child opened the install database")
        return real(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", guarded)

    def no_store(*a, **kw):
        raise AssertionError("service child built an install-db store")

    monkeypatch.setattr(secret_refs, "SqliteInstallCredentialStore", no_store)


@pytest.mark.parametrize("env_name", [*sorted(BROKER_ENV_NAMES), "FABRI_SESSION_ID"])
def test_service_child_never_opens_the_db(home, monkeypatch, env_name):
    _connect(home)
    monkeypatch.setenv(env_name, "set-by-the-launcher")
    _forbid_db(monkeypatch, home)
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert "value" not in seen
    assert "error" in seen
    assert secret_refs._cli_install_db is None


def test_store_refuses_db_if_broker_env_appears_later(home, monkeypatch):
    # Even with the process-level path set, a broker env at lookup time wins.
    _connect(home)
    secret_refs.use_cli_install_db(home / "serve" / "installs.db")
    assert secret_refs._cli_install_db is not None
    monkeypatch.setenv(sorted(BROKER_ENV_NAMES)[0], "/tmp/broker.sock")
    _forbid_db(monkeypatch, home)
    with pytest.raises(CredentialNotFoundError):
        resolve_secret("linear:acme")


def test_explicit_install_db_env_still_wins(home, tmp_path, monkeypatch):
    _connect(home)
    other = tmp_path / "other.db"
    monkeypatch.setenv("FABRI_INSTALL_DB", str(other))
    store = cli._connection_store(argparse.Namespace(home_root=None), create=True)
    store.upsert(provider="linear", handle="acme", access_token="other-db-token-77")
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert seen == {"value": "other-db-token-77"}
