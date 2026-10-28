"""Linear access tokens last 24 hours. The install store keeps the rotating
refresh token and swaps in a new access token before the old one expires."""
from __future__ import annotations

import sqlite3

import pytest

from fabri.service import linear_oauth
from fabri.service.install_store import REFRESH_MARGIN_S, LinearInstallStore
from fabri.tools.credential_store import SqliteInstallCredentialStore

pytestmark = pytest.mark.unit
T0 = 1_000_000.0


def _store(tmp_path, refresher=None):
    store = LinearInstallStore(tmp_path / "installs.db", refresher=refresher)
    store.upsert(workspace_id="ws1", access_token="old-access", refresh_token="r1", expires_in=86399, now=T0)
    return store


def test_fresh_token_is_returned_without_refresh(tmp_path):
    calls = []
    store = _store(tmp_path, refresher=lambda r: calls.append(r) or {})
    assert store.get_token("ws1", now=T0 + 3600) == "old-access" and calls == []


def test_token_near_expiry_is_refreshed_and_rotation_stored(tmp_path):
    calls = []

    def refresher(refresh_token):
        calls.append(refresh_token)
        return {"access_token": "new-access", "refresh_token": f"r{len(calls) + 1}", "expires_in": 86399}

    store = _store(tmp_path, refresher)
    due = T0 + 86399 - REFRESH_MARGIN_S + 1
    assert store.get_token("ws1", now=due) == "new-access"
    assert store.get_token("ws1", now=due + 10) == "new-access"
    assert calls == ["r1"]
    later = due + 86399
    assert store.get_token("ws1", now=later) == "new-access" and calls == ["r1", "r2"]
    with sqlite3.connect(tmp_path / "installs.db") as c:
        [(refresh_token, expires_at)] = c.execute("SELECT refresh_token, expires_at FROM linear_installs").fetchall()
    assert refresh_token == "r3" and expires_at == pytest.approx(later + 86399)


def test_failed_refresh_keeps_a_live_token_and_drops_a_dead_one(tmp_path):
    def broken(_):
        raise OSError("network down")

    store = _store(tmp_path, broken)
    assert store.get_token("ws1", now=T0 + 86399 - 10) == "old-access"
    assert store.get_token("ws1", now=T0 + 90000) is None


def test_expired_token_without_refresher_is_not_handed_out(tmp_path):
    store = _store(tmp_path)
    assert store.get_token("ws1", now=T0 + 90000) is None


def test_old_rows_without_expiry_still_work(tmp_path):
    db = tmp_path / "installs.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE linear_installs(workspace_id TEXT PRIMARY KEY, access_token TEXT NOT NULL, "
                  "workspace_name TEXT, scopes TEXT, installed_at REAL, updated_at REAL)")
        c.execute("INSERT INTO linear_installs VALUES('ws0','legacy',NULL,NULL,1,1)")
    store = LinearInstallStore(db, refresher=lambda r: pytest.fail("no refresh for legacy rows"))
    assert store.get_token("ws0") == "legacy"


def test_credential_store_refreshes_through_the_install_store(tmp_path, monkeypatch):
    _store(tmp_path)
    monkeypatch.setattr(linear_oauth, "refresh_access_token",
                        lambda r: {"access_token": "via-cred-store", "refresh_token": "r2", "expires_in": 86399})
    creds = SqliteInstallCredentialStore(tmp_path / "installs.db")
    assert creds.get("linear", "ws1") == "via-cred-store"


def test_refresh_request_shape(monkeypatch):
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token": "a2", "refresh_token": "r2", "expires_in": 86399}'

    def fake_open(request, timeout):
        sent["url"], sent["body"] = request.full_url, request.data.decode()
        return _Resp()

    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(linear_oauth._opener, "open", fake_open)
    assert linear_oauth.refresh_access_token("r1")["access_token"] == "a2"
    assert sent["url"] == "https://api.linear.app/oauth/token"
    assert "grant_type=refresh_token" in sent["body"] and "refresh_token=r1" in sent["body"]
