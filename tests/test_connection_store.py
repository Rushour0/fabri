"""ConnectionStore: one table of vendor credentials in installs.db."""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading

import pytest

from fabri.service import connection_store as cs_module
from fabri.service.connection_store import REFRESH_MARGIN_S, ConnectionStore
from fabri.service.install_store import (
    REFRESH_MARGIN_S as LEGACY_MARGIN,
    LinearInstallStore,
    SlackInstallStore,
)

pytestmark = pytest.mark.unit
T0 = 1_000_000.0


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk_keys(inner)


def test_file_is_created_0600_and_wal_too(tmp_path):
    db = tmp_path / "installs.db"
    old = os.umask(0o022)
    try:
        store = ConnectionStore(db)
        store.upsert(provider="cakemail", handle="main", access_token="tok-a", now=T0)
    finally:
        os.umask(old)
    assert _mode(db) == 0o600
    wal = tmp_path / "installs.db-wal"
    # Keep a reader open so the WAL file stays on disk, widen it, and reopen:
    # the store must tighten it again.
    holder = sqlite3.connect(db)
    try:
        holder.execute("SELECT 1 FROM connections").fetchall()
        assert wal.exists()
        os.chmod(wal, 0o644)
        ConnectionStore(db)
        assert _mode(wal) == 0o600
        shm = tmp_path / "installs.db-shm"
        if shm.exists():
            assert _mode(shm) == 0o600
    finally:
        holder.close()


def test_existing_wider_file_is_tightened(tmp_path):
    db = tmp_path / "installs.db"
    db.touch()
    os.chmod(db, 0o644)
    ConnectionStore(db)
    assert _mode(db) == 0o600


def test_upsert_keeps_installed_at_and_replaces_tokens(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db", refreshers={"linear": lambda r: {}})
    store.upsert(
        provider="linear", handle="ws1", access_token="oauth-access",
        refresh_token="r1", expires_in=86399, scopes="read", display_name="WS",
        meta={"source": "oauth"}, now=T0,
    )
    # A pasted static token replaces the OAuth row whole.
    store.upsert(provider="linear", handle="ws1", access_token="pasted", now=T0 + 50)
    row = store.get("linear", "ws1")
    assert row == {
        "provider": "linear", "handle": "ws1", "scope_kind": "org", "scopes": None,
        "display_name": None, "status": "ok", "expires_at": None,
        "installed_at": T0, "updated_at": T0 + 50, "meta": None,
    }
    assert store.get_token("linear", "ws1", now=T0 + 10**7) == "pasted"
    with sqlite3.connect(store.path) as c:
        [(refresh_token,)] = c.execute("SELECT refresh_token FROM connections").fetchall()
    assert refresh_token is None


def test_expires_in_zero_means_no_expiry(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="cakemail", handle="main", access_token="pat", expires_in=0, now=T0)
    assert store.get("cakemail", "main")["expires_at"] is None
    assert store.get_token("cakemail", "main", now=T0 + 10**9) == "pat"


def test_rows_never_carry_tokens(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(
        provider="linear", handle="ws1", access_token="secret-a", refresh_token="secret-r",
        expires_in=100, meta={"source": "oauth"}, now=T0,
    )
    store.upsert(provider="gmail", handle="u1", access_token="secret-g", scope_kind="user", now=T0)
    rows = [store.get("linear", "ws1"), store.get("gmail", "u1"), *store.list(),
            *store.list("gmail", scope_kind="user", handle="u1")]
    assert len(rows) == 5
    for row in rows:
        assert not [k for k in _walk_keys(row) if "token" in k.lower()]
        assert "secret" not in repr(row)


def test_list_filters_and_scope_kind(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="gmail", handle="u1", access_token="a", scope_kind="user", now=T0)
    store.upsert(provider="gmail", handle="main", access_token="b", now=T0)
    store.upsert(provider="cakemail", handle="main", access_token="c", now=T0)
    assert [(r["provider"], r["handle"]) for r in store.list()] == [
        ("cakemail", "main"), ("gmail", "main"), ("gmail", "u1")]
    assert [r["handle"] for r in store.list("gmail", scope_kind="user")] == ["u1"]
    assert [r["provider"] for r in store.list(handle="main")] == ["cakemail", "gmail"]
    assert store.scope_kind("gmail", "u1") == "user"
    assert store.scope_kind("gmail", "main") == "org"
    assert store.scope_kind("gmail", "nobody") is None
    with pytest.raises(ValueError):
        store.upsert(provider="gmail", handle="x", access_token="a", scope_kind="team")


def test_delete_and_set_status(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="cakemail", handle="main", access_token="a", now=T0)
    assert store.set_status("cakemail", "main", "revoked") is True
    assert store.get("cakemail", "main")["status"] == "revoked"
    assert store.set_status("cakemail", "other", "revoked") is False
    assert store.delete("cakemail", "main") is True
    assert store.delete("cakemail", "main") is False
    assert store.get("cakemail", "main") is None
    assert store.get_token("cakemail", "main") is None


def _linear(tmp_path, refresher):
    store = ConnectionStore(tmp_path / "installs.db", refreshers={"linear": refresher})
    store.upsert(provider="linear", handle="ws1", access_token="old-access",
                 refresh_token="r1", expires_in=86399, now=T0)
    return store


def test_get_token_refreshes_near_expiry_and_stores_rotation(tmp_path):
    calls = []

    def refresher(refresh_token):
        calls.append(refresh_token)
        return {"access_token": f"new-{len(calls)}", "refresh_token": f"r{len(calls) + 1}",
                "expires_in": 86399}

    store = _linear(tmp_path, refresher)
    assert store.get_token("linear", "ws1", now=T0 + 3600) == "old-access" and calls == []
    due = T0 + 86399 - REFRESH_MARGIN_S + 1
    assert store.get_token("linear", "ws1", now=due) == "new-1"
    assert store.get_token("linear", "ws1", now=due + 10) == "new-1"
    assert calls == ["r1"]
    with sqlite3.connect(store.path) as c:
        [(refresh_token, expires_at)] = c.execute(
            "SELECT refresh_token, expires_at FROM connections").fetchall()
    assert refresh_token == "r2" and expires_at == pytest.approx(due + 86399)
    assert store.get("linear", "ws1")["updated_at"] == due
    # A response without a new refresh token keeps the old one.
    store.refreshers["linear"] = lambda r: {"access_token": "new-x"}
    assert store.get_token("linear", "ws1", now=due + 86399) == "new-x"
    with sqlite3.connect(store.path) as c:
        [(refresh_token, expires_at)] = c.execute(
            "SELECT refresh_token, expires_at FROM connections").fetchall()
    assert refresh_token == "r2" and expires_at is None


def test_failed_refresh_keeps_live_token_and_drops_dead_one(tmp_path, caplog):
    def broken(_):
        raise OSError("network down old-access r1")

    store = _linear(tmp_path, broken)
    with caplog.at_level(logging.DEBUG, logger="fabri.service.connection_store"):
        assert store.get_token("linear", "ws1", now=T0 + 86399 - 10) == "old-access"
        assert store.get_token("linear", "ws1", now=T0 + 90000) is None
    with sqlite3.connect(store.path) as c:
        [(access, refresh, expires_at)] = c.execute(
            "SELECT access_token, refresh_token, expires_at FROM connections").fetchall()
    assert (access, refresh, expires_at) == ("old-access", "r1", pytest.approx(T0 + 86399))
    assert "OSError" in caplog.text
    assert "old-access" not in caplog.text and "r1" not in caplog.text


def test_refresh_does_not_hold_the_write_lock_during_the_vendor_call(tmp_path):
    other_writes = []

    def refresher(refresh_token):
        # Under BEGIN IMMEDIATE for the whole refresh this write would wait
        # out its timeout and fail with "database is locked".
        with sqlite3.connect(store.path, timeout=0.2, isolation_level=None) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE connections SET display_name='during' WHERE provider='linear'")
            c.execute("COMMIT")
        c.close()
        other_writes.append(True)
        return {"access_token": "new-access", "refresh_token": "r2", "expires_in": 86399}

    store = _linear(tmp_path, refresher)
    due = T0 + 86399 - REFRESH_MARGIN_S + 1
    assert store.get_token("linear", "ws1", now=due) == "new-access"
    assert other_writes == [True]
    with sqlite3.connect(store.path) as c:
        [(refresh_token, lease)] = c.execute(
            "SELECT refresh_token, refresh_lease_until FROM connections").fetchall()
    c.close()
    assert refresh_token == "r2" and lease is None


def test_concurrent_refresh_spends_the_refresh_token_once(tmp_path):
    calls, entered, release = [], threading.Event(), threading.Event()

    def refresher(refresh_token):
        calls.append(refresh_token)
        entered.set()
        assert release.wait(5)
        return {"access_token": "new-access", "refresh_token": "r2", "expires_in": 86399}

    store = _linear(tmp_path, refresher)
    expired = T0 + 86399 + 10
    results = {}
    first = threading.Thread(target=lambda: results.update(a=store.get_token("linear", "ws1", now=expired)))
    first.start()
    assert entered.wait(5)
    # A second caller, here a second store on the same file, finds the lease
    # and waits for the result instead of spending r1 again.
    other = ConnectionStore(store.path, refreshers={"linear": refresher})
    second = threading.Thread(target=lambda: results.update(b=other.get_token("linear", "ws1", now=expired)))
    second.start()
    release.set()
    first.join(10)
    second.join(10)
    assert results == {"a": "new-access", "b": "new-access"}
    assert calls == ["r1"]


def test_refresh_result_is_dropped_when_the_row_changed_meanwhile(tmp_path):
    def refresher(refresh_token):
        store.upsert(provider="linear", handle="ws1", access_token="reinstalled",
                     refresh_token="r-new", expires_in=86399, now=due)
        return {"access_token": "from-refresh", "refresh_token": "r2", "expires_in": 86399}

    store = _linear(tmp_path, refresher)
    due = T0 + 86399 - REFRESH_MARGIN_S + 1
    assert store.get_token("linear", "ws1", now=due) == "reinstalled"
    with sqlite3.connect(store.path) as c:
        [(access, refresh_token, lease)] = c.execute(
            "SELECT access_token, refresh_token, refresh_lease_until FROM connections").fetchall()
    c.close()
    assert (access, refresh_token, lease) == ("reinstalled", "r-new", None)


def test_a_live_lease_skips_the_refresh_and_a_stale_one_does_not(tmp_path):
    calls = []
    store = _linear(tmp_path, lambda r: calls.append(r) or {"access_token": "new-access"})
    due = T0 + 86399 - REFRESH_MARGIN_S + 1
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE connections SET refresh_lease_until=?", (due + 30,))
    c.close()
    # Another caller holds the lease and the token is still valid: use it.
    assert store.get_token("linear", "ws1", now=due) == "old-access" and calls == []
    # The lease ran out (its holder died): this caller takes it over.
    assert store.get_token("linear", "ws1", now=due + 31) == "new-access" and calls == ["r1"]


def test_expired_token_without_refresher_is_not_handed_out(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="linear", handle="ws1", access_token="old",
                 refresh_token="r1", expires_in=100, now=T0)
    assert store.get_token("linear", "ws1", now=T0 + 50) == "old"
    assert store.get_token("linear", "ws1", now=T0 + 101) is None


def test_revoked_status_returns_none_without_refresh(tmp_path):
    store = _linear(tmp_path, lambda r: pytest.fail("revoked rows are never refreshed"))
    store.set_status("linear", "ws1", "revoked")
    assert store.get_token("linear", "ws1", now=T0 + 10) is None
    assert store.get_token("linear", "ws1", now=T0 + 86399) is None


_V027_SLACK = """CREATE TABLE installs(
    team_id TEXT PRIMARY KEY, bot_token TEXT NOT NULL, team_name TEXT,
    scopes TEXT, installed_at REAL, updated_at REAL)"""
_V027_LINEAR = """CREATE TABLE linear_installs(
    workspace_id TEXT PRIMARY KEY, access_token TEXT NOT NULL, workspace_name TEXT,
    scopes TEXT, installed_at REAL, updated_at REAL)"""


def _legacy_db(db, *, linear_expiry_columns=True):
    with sqlite3.connect(db) as c:
        c.execute(_V027_SLACK)
        c.execute(_V027_LINEAR)
        if linear_expiry_columns:
            c.execute("ALTER TABLE linear_installs ADD COLUMN refresh_token TEXT")
            c.execute("ALTER TABLE linear_installs ADD COLUMN expires_at REAL")
            c.execute("INSERT INTO linear_installs VALUES('ws1','lin-tok','WS One','read',1,2,'r1',99999)")
        else:
            c.execute("INSERT INTO linear_installs VALUES('ws1','lin-tok','WS One','read',1,2)")
        c.execute("INSERT INTO installs VALUES('T1','xoxb-1','Team One','chat:write',10,20)")
        c.execute("INSERT INTO installs VALUES('T2','xoxb-2',NULL,NULL,30,40)")
    c.close()


def _types(db):
    with sqlite3.connect(db) as c:
        return dict(c.execute(
            "SELECT name, type FROM sqlite_master WHERE name IN ('installs','linear_installs')"
        ).fetchall())


def test_migrates_slack_and_linear_tables_once_and_leaves_views(tmp_path):
    db = tmp_path / "installs.db"
    _legacy_db(db)
    store = ConnectionStore(db)
    assert _types(db) == {"installs": "view", "linear_installs": "view"}
    assert store.list() == [
        {"provider": "linear", "handle": "ws1", "scope_kind": "org", "scopes": "read",
         "display_name": "WS One", "status": "ok", "expires_at": 99999.0,
         "installed_at": 1.0, "updated_at": 2.0, "meta": None},
        {"provider": "slack", "handle": "T1", "scope_kind": "org", "scopes": "chat:write",
         "display_name": "Team One", "status": "ok", "expires_at": None,
         "installed_at": 10.0, "updated_at": 20.0, "meta": None},
        {"provider": "slack", "handle": "T2", "scope_kind": "org", "scopes": None,
         "display_name": None, "status": "ok", "expires_at": None,
         "installed_at": 30.0, "updated_at": 40.0, "meta": None},
    ]
    assert store.get_token("slack", "T1") == "xoxb-1"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT bot_token FROM installs WHERE team_id='T2'").fetchone() == ("xoxb-2",)
        assert c.execute(
            "SELECT access_token, refresh_token, expires_at FROM linear_installs"
        ).fetchall() == [("lin-tok", "r1", 99999.0)]
        # An older fabri runs CREATE TABLE IF NOT EXISTS; it is a no-op on a view.
        c.execute(_V027_SLACK.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
    c.close()
    # Second start: nothing migrates again and nothing duplicates.
    again = ConnectionStore(db)
    SlackInstallStore(db)
    LinearInstallStore(db)
    assert len(again.list()) == 3
    assert _types(db) == {"installs": "view", "linear_installs": "view"}
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM connections").fetchone() == (3,)
    c.close()


def test_migration_handles_linear_table_without_expiry_columns(tmp_path):
    db = tmp_path / "installs.db"
    _legacy_db(db, linear_expiry_columns=False)
    store = ConnectionStore(db, refreshers={"linear": lambda r: pytest.fail("no refresh")})
    assert store.get_token("linear", "ws1") == "lin-tok"
    assert store.get("linear", "ws1")["expires_at"] is None


def test_migration_keeps_existing_connection_rows(tmp_path):
    # A connections row for the same key wins over the legacy copy.
    db = tmp_path / "installs.db"
    with sqlite3.connect(db) as c:
        c.execute(_V027_SLACK)
        c.execute("INSERT INTO installs VALUES('T1','xoxb-legacy','Old',NULL,1,1)")
    c.close()
    os.chmod(db, 0o600)
    # Build connections by hand first, as if a half-finished run had made it.
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE connections(
            provider TEXT NOT NULL, handle TEXT NOT NULL, scope_kind TEXT NOT NULL DEFAULT 'org',
            access_token TEXT NOT NULL, refresh_token TEXT, expires_at REAL, scopes TEXT,
            display_name TEXT, status TEXT NOT NULL DEFAULT 'ok', meta TEXT,
            installed_at REAL, updated_at REAL, PRIMARY KEY(provider, handle))""")
        c.execute("INSERT INTO connections(provider, handle, access_token, installed_at, updated_at) "
                  "VALUES('slack','T1','xoxb-new',5,5)")
    c.close()
    store = ConnectionStore(db)
    assert store.get_token("slack", "T1") == "xoxb-new"
    assert _types(db)["installs"] == "view"


def test_initialize_is_idempotent_on_a_fresh_database(tmp_path):
    db = tmp_path / "installs.db"
    for _ in range(3):
        ConnectionStore(db)
    assert _types(db) == {"installs": "view", "linear_installs": "view"}
    with sqlite3.connect(db) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    c.close()
    assert names.count("connections") == 1


def test_legacy_wrappers_round_trip(tmp_path):
    db = tmp_path / "installs.db"
    slack = SlackInstallStore(db)
    linear = LinearInstallStore(db)
    slack.upsert(team_id="T1", bot_token="xoxb-1", team_name="Team", scopes="chat:write", now=10.0)
    linear.upsert(workspace_id="ws1", access_token="lin-1", workspace_name="WS",
                  scopes="read", refresh_token="r1", expires_in=86399, now=T0)
    store = ConnectionStore(db)
    assert {(r["provider"], r["handle"]) for r in store.list()} == {("slack", "T1"), ("linear", "ws1")}
    assert store.get_token("slack", "T1") == "xoxb-1"
    assert store.get_token("linear", "ws1", now=T0) == "lin-1"
    # What `fabri installs list` prints is unchanged.
    assert SlackInstallStore(db).list() == [{"team_id": "T1", "team_name": "Team",
                                             "scopes": "chat:write", "installed_at": 10.0,
                                             "updated_at": 10.0}]
    assert LinearInstallStore(db).list() == [{"workspace_id": "ws1", "workspace_name": "WS",
                                              "scopes": "read", "installed_at": T0,
                                              "updated_at": T0}]
    # Writes through the new store show up in the wrappers.
    store.upsert(provider="slack", handle="T2", access_token="xoxb-2", display_name="Two", now=20.0)
    assert slack.get("T2") == {"team_id": "T2", "team_name": "Two", "scopes": None,
                               "installed_at": 20.0, "updated_at": 20.0}
    assert slack.get_token("T2") == "xoxb-2"
    assert slack.delete("T2") is True and store.get("slack", "T2") is None
    assert linear.delete("ws1") is True and store.get_token("linear", "ws1") is None
    assert LEGACY_MARGIN == REFRESH_MARGIN_S
    assert linear.refresher is None


def test_permissions_warning_logged_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(cs_module, "_PERMS_WARNED", set())
    db = tmp_path / "installs.db"
    ConnectionStore(db)
    os.chmod(db, 0o644)
    with caplog.at_level(logging.WARNING, logger="fabri.service.connection_store"):
        ConnectionStore(db)
        os.chmod(db, 0o644)
        ConnectionStore(db)
    warnings = [r for r in caplog.records if "wider than 0600" in r.getMessage()]
    assert len(warnings) == 1
    assert _mode(db) == 0o600


def test_permissions_warning_for_group_writable_directory(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(cs_module, "_PERMS_WARNED", set())
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0o775)
    with caplog.at_level(logging.WARNING, logger="fabri.service.connection_store"):
        store = ConnectionStore(home / "installs.db")
    assert "installs.db permissions are wider than 0600" in caplog.text
    assert store.permissions_too_wide() is True
    os.chmod(home, 0o700)
    assert store.permissions_too_wide() is False


def test_repr_has_no_token(tmp_path, caplog):
    db = tmp_path / "installs.db"
    with caplog.at_level(logging.DEBUG):
        store = ConnectionStore(db, refreshers={"linear": lambda r: {"access_token": "fresh-secret"}})
        store.upsert(provider="linear", handle="ws1", access_token="stored-secret",
                     refresh_token="refresh-secret", expires_in=10, now=T0)
        store.get_token("linear", "ws1", now=T0 + 5)
        store.delete("linear", "ws1")
    assert repr(store) == f"ConnectionStore({str(db)!r})"
    for secret in ("stored-secret", "refresh-secret", "fresh-secret"):
        assert secret not in repr(store)
        assert secret not in caplog.text
        assert secret not in repr(vars(store))


def _pending(store, nonce, *, now, scope_kind="org", user_id=None):
    store.pending_put(nonce=nonce, provider="gmail", handle=None, scope_kind=scope_kind,
                      user_id=user_id, code_verifier=f"verifier-{nonce}", scopes="a b",
                      redirect_uri="https://x.test/cb", now=now)


def test_pending_pop_is_single_use_and_keeps_fields(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    _pending(store, "n1", now=T0, scope_kind="user", user_id="u1")
    row = store.pending_pop("n1", now=T0 + 10)
    assert row["provider"] == "gmail" and row["scope_kind"] == "user" and row["user_id"] == "u1"
    assert row["code_verifier"] == "verifier-n1" and row["redirect_uri"] == "https://x.test/cb"
    assert row["expires_at"] == T0 + cs_module.PENDING_TTL_S
    assert store.pending_pop("n1", now=T0 + 11) is None
    assert store.pending_pop("never", now=T0) is None
    # Pending handshakes are not connections.
    assert store.list() == []
    with pytest.raises(ValueError):
        _pending(store, "n2", now=T0, scope_kind="team")


def test_pending_pop_check_refusal_leaves_the_row(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    _pending(store, "n1", now=T0, scope_kind="user", user_id="u1")
    seen = []

    def not_yours(row):
        seen.append(row["user_id"])
        raise PermissionError("someone else's flow")

    with pytest.raises(PermissionError):
        store.pending_pop("n1", now=T0 + 1, check=not_yours)
    assert seen == ["u1"]
    # The owner's check passes and takes the row, once.
    row = store.pending_pop("n1", now=T0 + 2, check=lambda r: None)
    assert row["user_id"] == "u1"
    assert store.pending_pop("n1", now=T0 + 3, check=lambda r: None) is None
    # An expired row is removed without calling the check.
    _pending(store, "old", now=T0)
    assert store.pending_pop("old", now=T0 + cs_module.PENDING_TTL_S + 1,
                             check=lambda r: pytest.fail("no check for expired rows")) is None


def test_upsert_keep_scope_refuses_a_scope_change(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    store.upsert(provider="acme", handle="u1", access_token="org-token")
    with pytest.raises(cs_module.ScopeConflict):
        store.upsert(provider="acme", handle="u1", access_token="user-token", scope_kind="user",
                     keep_scope=True)
    assert store.get_token("acme", "u1") == "org-token"
    store.upsert(provider="acme", handle="u1", access_token="org-2", keep_scope=True)
    assert store.get_token("acme", "u1") == "org-2"
    # Without keep_scope the older replace-whole rule still applies.
    store.upsert(provider="acme", handle="u1", access_token="user-token", scope_kind="user")
    assert store.scope_kind("acme", "u1") == "user"


def test_pending_pop_of_expired_nonce_deletes_it(tmp_path):
    store = ConnectionStore(tmp_path / "installs.db")
    _pending(store, "old", now=T0)
    assert store.pending_pop("old", now=T0 + cs_module.PENDING_TTL_S + 1) is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM pending_oauth").fetchone() == (0,)
    c.close()
    # A later put sweeps rows that are already past their expiry.
    _pending(store, "stale", now=T0)
    _pending(store, "fresh", now=T0 + cs_module.PENDING_TTL_S + 1)
    with sqlite3.connect(store.path) as c:
        assert [r[0] for r in c.execute("SELECT nonce FROM pending_oauth")] == ["fresh"]
    c.close()


def test_state_secret_is_shared_by_two_stores_on_one_file(tmp_path, monkeypatch):
    monkeypatch.delenv(cs_module.STATE_SECRET_ENV, raising=False)
    db = tmp_path / "installs.db"
    a, b = ConnectionStore(db), ConnectionStore(db)
    assert a.state_secret() == b.state_secret() == a.state_secret()
    assert len(a.state_secret()) >= 40
    assert a.state_secret() not in repr(a)
    assert _mode(db) == 0o600


def test_wave_four_tables_exist_after_migration(tmp_path):
    db = tmp_path / "installs.db"
    _legacy_db(db)
    ConnectionStore(db)
    with sqlite3.connect(db) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert {"connections", "service_secrets", "pending_oauth"} <= tables
