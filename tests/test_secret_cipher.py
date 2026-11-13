"""Encryption at rest for installs.db (fabri.secret_cipher + ConnectionStore)."""
from __future__ import annotations

import sqlite3
import sys

import pytest

from fabri import cli
from fabri import secret_cipher as sc
from fabri.secret_cipher import PlaintextRefused, SecretCipher, SecretKeyError, generate_key
from fabri.service.connection_store import ConnectionStore

pytestmark = pytest.mark.unit
T0 = 1_000_000.0
ACCESS = "xoxb-access-PLAINTEXT-1234"
REFRESH = "refresh-PLAINTEXT-5678"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (sc.SECRET_KEY_ENV, sc.SECRET_KEY_FILE_ENV, sc.ALLOW_PLAINTEXT_ENV,
                 "FABRI_OAUTH_STATE_SECRET", "FABRI_INSTALL_DB"):
        monkeypatch.delenv(name, raising=False)
    sc.require_encryption(None)
    yield
    sc.require_encryption(None)


@pytest.fixture
def key():
    return generate_key()


def raw_rows(db) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT access_token, refresh_token FROM connections").fetchall()


def test_cipher_round_trip_and_plaintext_passthrough(key):
    cipher = SecretCipher(key)
    value = cipher.encrypt("hello")
    assert value.startswith(sc.PREFIX) and "hello" not in value
    assert cipher.decrypt(value) == "hello"
    assert cipher.decrypt("old-plain") == "old-plain"
    assert repr(cipher) == "SecretCipher(keys=1)"


def test_store_round_trip_and_file_has_no_plaintext(tmp_path, monkeypatch, key):
    monkeypatch.setenv(sc.SECRET_KEY_ENV, key)
    db = tmp_path / "installs.db"
    store = ConnectionStore(db)
    assert store.encrypted
    store.upsert(provider="gmail", handle="u1", access_token=ACCESS, refresh_token=REFRESH, now=T0)
    store.pending_put(nonce="n1", provider="gmail", handle=None, scope_kind="user", user_id="u1",
                      code_verifier="VERIFIER-PLAINTEXT", scopes="read", redirect_uri="http://x/cb", now=T0)
    secret = store.state_secret()
    assert store.get_token("gmail", "u1", now=T0) == ACCESS
    access, refresh = raw_rows(db)[0]
    assert access.startswith(sc.PREFIX) and refresh.startswith(sc.PREFIX)
    for path in tmp_path.iterdir():
        blob = path.read_bytes()
        for plain in (ACCESS, REFRESH, "VERIFIER-PLAINTEXT", secret):
            assert plain.encode() not in blob, path.name
    assert store.pending_pop("n1", now=T0)["code_verifier"] == "VERIFIER-PLAINTEXT"
    assert ConnectionStore(db).state_secret() == secret


def test_refresh_under_encryption_spends_plaintext_and_stores_ciphertext(tmp_path, key):
    seen = []

    def refresher(token):
        seen.append(token)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    db = tmp_path / "installs.db"
    store = ConnectionStore(db, refreshers={"linear": refresher}, cipher=SecretCipher(key))
    store.upsert(provider="linear", handle="w", access_token=ACCESS, refresh_token=REFRESH,
                 expires_in=60, now=T0)
    assert store.get_token("linear", "w", now=T0) == "new-access"
    assert seen == [REFRESH]
    access, refresh = raw_rows(db)[0]
    assert access.startswith(sc.PREFIX) and refresh.startswith(sc.PREFIX)
    assert store.get_token("linear", "w", now=T0) == "new-access"


def test_wrong_key_fails_closed(tmp_path, key):
    db = tmp_path / "installs.db"
    ConnectionStore(db, cipher=SecretCipher(key)).upsert(provider="slack", handle="T1", access_token=ACCESS)
    other = ConnectionStore(db, cipher=SecretCipher(generate_key()))
    with pytest.raises(SecretKeyError) as exc:
        other.get_token("slack", "T1")
    assert key not in str(exc.value) and ACCESS not in str(exc.value)
    with pytest.raises(SecretKeyError):
        ConnectionStore(db, cipher=None).get_token("slack", "T1")


def test_malformed_key_is_refused_with_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv(sc.SECRET_KEY_ENV, "not-a-key")
    with pytest.raises(SecretKeyError, match="malformed") as exc:
        ConnectionStore(tmp_path / "installs.db")
    assert "not-a-key" not in str(exc.value)


def test_key_file(tmp_path, monkeypatch, key):
    path = tmp_path / "key"
    path.write_text(key + "\n")
    monkeypatch.setenv(sc.SECRET_KEY_FILE_ENV, str(path))
    assert sc.load_cipher() is not None
    path.write_text("")
    with pytest.raises(SecretKeyError, match="empty"):
        sc.load_cipher()


def test_old_plaintext_row_is_readable_and_reencrypted_on_write(tmp_path, key):
    db = tmp_path / "installs.db"
    ConnectionStore(db, cipher=None).upsert(provider="slack", handle="T1", access_token=ACCESS)
    assert raw_rows(db)[0][0] == ACCESS
    store = ConnectionStore(db, cipher=SecretCipher(key))
    assert store.get_token("slack", "T1") == ACCESS
    assert store.plaintext_count() == 1
    store.upsert(provider="slack", handle="T1", access_token=ACCESS)
    assert raw_rows(db)[0][0].startswith(sc.PREFIX)
    assert store.plaintext_count() == 0
    assert store.get_token("slack", "T1") == ACCESS


def test_rekey_encrypts_plaintext_and_rotates_old_key(tmp_path, key):
    db = tmp_path / "installs.db"
    plain = ConnectionStore(db, cipher=None)
    plain.upsert(provider="slack", handle="T1", access_token=ACCESS)
    plain.state_secret()
    old = generate_key()
    ConnectionStore(db, cipher=SecretCipher(old)).upsert(provider="linear", handle="w",
                                                          access_token="lin", refresh_token=REFRESH)
    rotated = ConnectionStore(db, cipher=SecretCipher(f"{key},{old}"))
    assert rotated.rekey() == {"rewritten": 3, "skipped": 0, "unreadable": []}
    assert rotated.rekey() == {"rewritten": 0, "skipped": 0, "unreadable": []}
    fresh = ConnectionStore(db, cipher=SecretCipher(key))
    assert fresh.get_token("slack", "T1") == ACCESS
    assert fresh.get_token("linear", "w") == "lin"
    with pytest.raises(SecretKeyError):
        ConnectionStore(db, cipher=None).rekey()


def test_no_key_stores_plaintext_and_list_warns(tmp_path, monkeypatch, capsys):
    db = tmp_path / "installs.db"
    store = ConnectionStore(db)
    assert not store.encrypted
    store.upsert(provider="slack", handle="T1", access_token=ACCESS)
    assert raw_rows(db)[0][0] == ACCESS
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setattr(sys, "argv", ["fabri", "connections", "list"])
    cli.main()
    err = capsys.readouterr().err
    assert "plaintext" in err and "FABRI_SECRET_KEY" in err


def test_cli_rekey_and_keygen(tmp_path, monkeypatch, capsys, key):
    db = tmp_path / "installs.db"
    ConnectionStore(db, cipher=None).upsert(provider="slack", handle="T1", access_token=ACCESS)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setenv(sc.SECRET_KEY_ENV, key)
    monkeypatch.setattr(sys, "argv", ["fabri", "connections", "rekey"])
    cli.main()
    assert "encrypted 1" in capsys.readouterr().out
    assert raw_rows(db)[0][0].startswith(sc.PREFIX)
    monkeypatch.setattr(sys, "argv", ["fabri", "connections", "keygen"])
    cli.main()
    SecretCipher(capsys.readouterr().out.strip())


def test_missing_cryptography_with_key_is_a_clear_error(tmp_path, monkeypatch, key):
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.fernet", None)
    monkeypatch.setenv(sc.SECRET_KEY_ENV, key)
    with pytest.raises(SecretKeyError, match=r"fabri\[encryption\]"):
        ConnectionStore(tmp_path / "installs.db")
    monkeypatch.delenv(sc.SECRET_KEY_ENV)
    ConnectionStore(tmp_path / "installs.db").upsert(provider="slack", handle="T1", access_token=ACCESS)


def test_hosted_guard_refuses_plaintext_writes_unless_opted_out(tmp_path, monkeypatch, key):
    store = ConnectionStore(tmp_path / "installs.db")
    sc.require_encryption("fabri serve is bound to the non-loopback address 0.0.0.0")
    with pytest.raises(PlaintextRefused, match="FABRI_SECRET_KEY"):
        store.upsert(provider="gmail", handle="u1", access_token=ACCESS)
    assert store.get("gmail", "u1") is None
    monkeypatch.setenv(sc.ALLOW_PLAINTEXT_ENV, "1")
    store.upsert(provider="gmail", handle="u1", access_token=ACCESS)
    monkeypatch.delenv(sc.ALLOW_PLAINTEXT_ENV)
    ConnectionStore(tmp_path / "installs.db", cipher=SecretCipher(key)).upsert(
        provider="gmail", handle="u2", access_token=ACCESS)


def _serve(tmp_path, monkeypatch, *, host="127.0.0.1", base=None, key=None, allow=False):
    from fabri.service.http_server import serve_http
    from fabri.service.service import FabriService

    if base:
        monkeypatch.setenv("FABRI_PUBLIC_BASE_URL", base)
    else:
        monkeypatch.delenv("FABRI_PUBLIC_BASE_URL", raising=False)
    if key:
        monkeypatch.setenv(sc.SECRET_KEY_ENV, key)
    if allow:
        monkeypatch.setenv(sc.ALLOW_PLAINTEXT_ENV, "1")
    service = FabriService(home_root=tmp_path / "home")
    service.auth_enabled = bool(base)  # a hosted base URL needs auth on to start at all
    try:
        serve_http(service, host=host, port=0).server_close()
    finally:
        service.close()


def test_serve_http_on_loopback_does_not_arm_the_guard(tmp_path, monkeypatch):
    _serve(tmp_path, monkeypatch)
    assert sc.encryption_required() is None


@pytest.mark.parametrize("host,base", [("0.0.0.0", None), ("127.0.0.1", "https://fabri.example.test")])
def test_serve_http_reachable_by_others_refuses_to_start_without_a_key(tmp_path, monkeypatch, host, base):
    from fabri.service.http_server import StartRefused

    with pytest.raises(StartRefused, match="FABRI_SECRET_KEY"):
        _serve(tmp_path, monkeypatch, host=host, base=base)
    assert sc.encryption_required() is None


@pytest.mark.parametrize("host,base", [("0.0.0.0", None), ("127.0.0.1", "https://fabri.example.test")])
def test_serve_http_reachable_by_others_arms_with_a_key_or_opt_out(tmp_path, monkeypatch, key, host, base):
    _serve(tmp_path, monkeypatch, host=host, base=base, key=key)
    assert sc.encryption_required()
    sc.require_encryption(None)
    monkeypatch.delenv(sc.SECRET_KEY_ENV)
    _serve(tmp_path / "b", monkeypatch, host=host, base=base, allow=True)
    assert sc.encryption_required()


def test_guard_covers_every_secret_write(tmp_path):
    calls = []
    store = ConnectionStore(tmp_path / "installs.db", cipher=None,
                            refreshers={"linear": lambda rt: calls.append(rt) or {"access_token": "new"}})
    store.upsert(provider="linear", handle="w", access_token="a", refresh_token="r", expires_in=1, now=T0)
    sc.require_encryption("test")
    with pytest.raises(PlaintextRefused):
        store.get_token("linear", "w", now=T0)
    assert calls == []
    with pytest.raises(PlaintextRefused):
        store.state_secret()
    with pytest.raises(PlaintextRefused):
        store.pending_put(nonce="n", provider="linear", handle=None, scope_kind="org", user_id=None,
                          code_verifier="v", scopes="", redirect_uri="http://x/cb")
    sc.require_encryption(None)
    secret = store.state_secret()
    sc.require_encryption("test")
    assert store.state_secret() == secret  # reading an existing secret writes nothing


def test_rekey_leaves_no_plaintext_in_the_file_or_wal(tmp_path, key):
    db = tmp_path / "installs.db"
    plain = ConnectionStore(db, cipher=None)
    for i in range(5):
        plain.upsert(provider="linear", handle=f"h{i}", access_token=f"PLAINACCESS{i}" + "x" * 40,
                     refresh_token=f"PLAINREFRESH{i}" + "y" * 40, expires_in=3600)
    assert ConnectionStore(db, cipher=SecretCipher(key)).rekey()["rewritten"] == 5
    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert b"PLAINACCESS" not in blob and b"PLAINREFRESH" not in blob


def test_rekey_skips_unreadable_and_leased_rows(tmp_path, key, monkeypatch, capsys):
    db = tmp_path / "installs.db"
    ConnectionStore(db, cipher=SecretCipher(generate_key())).upsert(provider="slack", handle="lost",
                                                                    access_token="L")
    plain = ConnectionStore(db, cipher=None)
    plain.upsert(provider="slack", handle="plain", access_token=ACCESS)
    plain.upsert(provider="linear", handle="leased", access_token="a", refresh_token="r")
    with sqlite3.connect(db) as conn:
        # An expired lease still counts: only its holder may change the row.
        conn.execute("UPDATE connections SET refresh_lease_until=1 WHERE handle='leased'")
    store = ConnectionStore(db, cipher=SecretCipher(key))
    assert store.rekey() == {"rewritten": 1, "skipped": 1, "unreadable": ["slack:lost"]}
    assert store.get_token("slack", "plain") == ACCESS
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setenv(sc.SECRET_KEY_ENV, key)
    monkeypatch.setattr(sys, "argv", ["fabri", "connections", "rekey"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "slack:lost" in capsys.readouterr().err


def test_non_ascii_after_prefix_and_bad_key_file_are_clean_errors(tmp_path, key):
    cipher = SecretCipher(key)
    assert cipher.needs_rewrite(sc.PREFIX + "café") is True
    with pytest.raises(SecretKeyError):
        cipher.decrypt(sc.PREFIX + "café")
    path = tmp_path / "key.bin"
    path.write_bytes(b"\xff\xfe" + key.encode())
    with pytest.raises(SecretKeyError, match="UTF-8") as exc:
        sc.load_cipher({sc.SECRET_KEY_FILE_ENV: str(path)})
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
