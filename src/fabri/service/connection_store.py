"""One table of connections (provider, handle) to third-party accounts.

Every credential fabri holds for a vendor lives here: Slack bot tokens, Linear
OAuth tokens, pasted API keys and PATs. Secrets at rest are plaintext in a
0600 file in this release. Nothing in this module logs a token, and the row
dicts it returns never carry a token column.

The file is ``installs.db``. On first open the legacy ``installs`` (Slack) and
``linear_installs`` (Linear) tables are copied into ``connections`` and
replaced by read-only views of the same name, so an older fabri that opens the
database can still read them. Writers go through this class.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Refresh a token this many seconds before it expires.
REFRESH_MARGIN_S = 300
# How long a pending OAuth handshake stays valid (wave 4).
PENDING_TTL_S = 600
# Environment variable that pins the OAuth state signing secret (wave 4).
STATE_SECRET_ENV = "FABRI_OAUTH_STATE_SECRET"
# How long one caller may hold a connection's refresh lease. Longer than the
# refresh request's own timeout, so a live refresh never loses its lease.
REFRESH_LEASE_S = 60.0
# How long a caller whose token has already expired waits for another
# caller's refresh to land before giving up.
REFRESH_WAIT_S = 20.0

# refresh_token -> token response {access_token, refresh_token?, expires_in?}
Refresher = Callable[[str], dict]

SCOPE_KINDS = ("org", "user")


class ScopeConflict(ValueError):
    """An upsert that would change an existing row's scope kind."""

_ROW_COLUMNS = (
    "provider, handle, scope_kind, scopes, display_name, status, "
    "expires_at, installed_at, updated_at, meta"
)

_PERMS_WARNED: set[str] = set()
_PERMS_LOCK = Lock()

# Legacy table name -> (provider, view SQL, copy column map).
# The copy map is legacy column -> connections column. A legacy column that
# an old database lacks is copied as NULL.
_LEGACY = (
    (
        "installs",
        "slack",
        "CREATE VIEW installs AS SELECT handle AS team_id, access_token AS bot_token, "
        "display_name AS team_name, scopes, installed_at, updated_at "
        "FROM connections WHERE provider='slack'",
        (
            ("team_id", "handle"),
            ("bot_token", "access_token"),
            ("team_name", "display_name"),
            ("scopes", "scopes"),
            ("installed_at", "installed_at"),
            ("updated_at", "updated_at"),
        ),
    ),
    (
        "linear_installs",
        "linear",
        "CREATE VIEW linear_installs AS SELECT handle AS workspace_id, access_token, "
        "display_name AS workspace_name, scopes, installed_at, updated_at, "
        "refresh_token, expires_at FROM connections WHERE provider='linear'",
        (
            ("workspace_id", "handle"),
            ("access_token", "access_token"),
            ("workspace_name", "display_name"),
            ("scopes", "scopes"),
            ("installed_at", "installed_at"),
            ("updated_at", "updated_at"),
            ("refresh_token", "refresh_token"),
            ("expires_at", "expires_at"),
        ),
    ),
)


def default_refreshers() -> dict[str, Refresher]:
    """Token refreshers for every catalog connector whose OAuth tokens rotate.

    Each entry with ``oauth`` and ``oauth.refresh`` gets its generic
    ``OAuthCodeProvider.refresh``, keyed by the provider name its rows are
    stored under. Linear keeps ``linear_oauth.refresh_access_token``, which
    sends the same request and which existing callers and tests patch.

    Imported lazily so loading this module does not pull in the catalog or
    the OAuth helpers (the tools package imports this one), and so a patched
    refresher is seen on the next call.
    """
    from fabri.service import linear_oauth
    from fabri.tools.connections.oauth import OAuthCodeProvider
    from fabri.tools.connector_catalog import CONNECTORS

    refreshers: dict[str, Refresher] = {}
    for name, entry in CONNECTORS.items():
        if entry.oauth is not None and entry.oauth.refresh:
            refreshers[entry.secret_provider or name] = OAuthCodeProvider(entry).refresh
    if "linear" in refreshers:
        refreshers["linear"] = linear_oauth.refresh_access_token
    return refreshers


class ConnectionStore:
    """Connections keyed by (provider, handle).

    Secrets at rest are plaintext in a 0600 file in this release. A refresher
    per provider (a callable taking the refresh token and returning the
    vendor's token response) lets ``get_token`` swap in a new access token
    when the stored one is within REFRESH_MARGIN_S of expiring.
    """

    def __init__(
        self, path: str | Path, refreshers: Mapping[str, Refresher] | None = None
    ) -> None:
        self.path = Path(path)
        self.refreshers: dict[str, Refresher] = dict(refreshers or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secure_file()
        self._initialize()
        self._secure_sidecars()
        self._check_permissions()

    def __repr__(self) -> str:
        return f"ConnectionStore({str(self.path)!r})"

    # -- file handling -----------------------------------------------------

    def _secure_file(self) -> None:
        self._wider_before = False
        try:
            self._wider_before = bool(os.stat(self.path).st_mode & 0o077)
        except FileNotFoundError:
            pass
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Not the owner; _check_permissions reports the wider mode.
            pass

    def _secure_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            try:
                os.chmod(sidecar, 0o600)
            except FileNotFoundError:
                continue
            except OSError:
                pass

    def _check_permissions(self) -> None:
        try:
            file_wide = bool(os.stat(self.path).st_mode & 0o077)
            dir_mode = os.stat(self.path.parent).st_mode
        except OSError:
            return
        dir_wide = bool(dir_mode & (stat.S_IWGRP | stat.S_IWOTH))
        if not (self._wider_before or file_wide or dir_wide):
            return
        key = str(self.path.resolve())
        with _PERMS_LOCK:
            if key in _PERMS_WARNED:
                return
            _PERMS_WARNED.add(key)
        logger.warning("installs.db permissions are wider than 0600")

    def permissions_too_wide(self) -> bool:
        """True when the file or its directory can be read or written by others."""
        try:
            file_wide = bool(os.stat(self.path).st_mode & 0o077)
            dir_wide = bool(
                os.stat(self.path.parent).st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        except OSError:
            return False
        return file_wide or dir_wide

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS connections(
                        provider TEXT NOT NULL,
                        handle TEXT NOT NULL,
                        scope_kind TEXT NOT NULL DEFAULT 'org',
                        access_token TEXT NOT NULL,
                        refresh_token TEXT,
                        expires_at REAL,
                        scopes TEXT,
                        display_name TEXT,
                        status TEXT NOT NULL DEFAULT 'ok',
                        meta TEXT,
                        installed_at REAL,
                        updated_at REAL,
                        refresh_lease_until REAL,
                        PRIMARY KEY(provider, handle)
                    )
                    """
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(connections)")}
                if "refresh_lease_until" not in columns:
                    connection.execute("ALTER TABLE connections ADD COLUMN refresh_lease_until REAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS service_secrets("
                    "name TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_oauth(
                        nonce TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        handle TEXT,
                        scope_kind TEXT NOT NULL,
                        user_id TEXT,
                        code_verifier TEXT NOT NULL,
                        scopes TEXT,
                        redirect_uri TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                for name, provider, view_sql, columns in _LEGACY:
                    kind = connection.execute(
                        "SELECT type FROM sqlite_master WHERE name=?", (name,)
                    ).fetchone()
                    kind = kind[0] if kind is not None else None
                    if kind == "table":
                        self._migrate(connection, name, provider, columns)
                        connection.execute(f"DROP TABLE {name}")
                        kind = None
                    if kind is None:
                        connection.execute(view_sql)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _migrate(
        connection: sqlite3.Connection,
        table: str,
        provider: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        targets = ["provider", "scope_kind", "status"]
        sources = ["?", "'org'", "'ok'"]
        for legacy, target in columns:
            targets.append(target)
            sources.append(legacy if legacy in present else "NULL")
        connection.execute(
            f"INSERT OR IGNORE INTO connections({', '.join(targets)}) "
            f"SELECT {', '.join(sources)} FROM {table}",
            (provider,),
        )

    # -- writes ------------------------------------------------------------

    def upsert(
        self,
        *,
        provider: str,
        handle: str,
        access_token: str,
        scope_kind: str = "org",
        refresh_token: str | None = None,
        expires_in: float | None = None,
        scopes: str | None = None,
        display_name: str | None = None,
        meta: Mapping | None = None,
        status: str = "ok",
        now: float | None = None,
        keep_scope: bool = False,
    ) -> None:
        """Insert or replace one connection.

        On conflict ``installed_at`` is kept and every other column is
        replaced with what is passed, ``refresh_token`` and ``expires_at``
        included (None clears them). A pasted static token therefore replaces
        an OAuth row whole, refresh token and expiry gone. ``expires_in`` of
        None or 0 means the token does not expire. Any refresh lease on the
        old row is dropped.

        With ``keep_scope`` the existing row's scope kind is read in the same
        write transaction, and ScopeConflict is raised (nothing written) when
        it differs from ``scope_kind``. A user flow then cannot replace an org
        row that has the same handle, nor the reverse.
        """
        if scope_kind not in SCOPE_KINDS:
            raise ValueError("scope_kind must be 'org' or 'user'")
        if not access_token:
            raise ValueError("access_token must not be empty")
        now = time.time() if now is None else now
        expires_at = now + float(expires_in) if expires_in else None
        meta_json = json.dumps(dict(meta)) if meta is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if keep_scope:
                    existing = connection.execute(
                        "SELECT scope_kind FROM connections WHERE provider=? AND handle=?",
                        (provider, handle),
                    ).fetchone()
                    if existing is not None and existing["scope_kind"] != scope_kind:
                        raise ScopeConflict(
                            f"{provider}:{handle} exists with another scope; remove it first"
                        )
                self._write_row(
                    connection, provider, handle, scope_kind, access_token, refresh_token,
                    expires_at, scopes, display_name, status, meta_json, now,
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        self._secure_sidecars()

    @staticmethod
    def _write_row(
        connection: sqlite3.Connection, provider: str, handle: str, scope_kind: str,
        access_token: str, refresh_token: str | None, expires_at: float | None,
        scopes: str | None, display_name: str | None, status: str, meta_json: str | None,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO connections(
                provider, handle, scope_kind, access_token, refresh_token,
                expires_at, scopes, display_name, status, meta,
                installed_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider, handle) DO UPDATE SET
                scope_kind=excluded.scope_kind,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                scopes=excluded.scopes,
                display_name=excluded.display_name,
                status=excluded.status,
                meta=excluded.meta,
                updated_at=excluded.updated_at,
                refresh_lease_until=NULL
            """,
            (
                provider, handle, scope_kind, access_token, refresh_token,
                expires_at, scopes, display_name, status, meta_json,
                now, now,
            ),
        )

    def delete(self, provider: str, handle: str) -> bool:
        with self._connect() as connection:
            cur = connection.execute(
                "DELETE FROM connections WHERE provider=? AND handle=?",
                (provider, handle),
            )
        return cur.rowcount > 0

    def set_status(self, provider: str, handle: str, status: str) -> bool:
        with self._connect() as connection:
            cur = connection.execute(
                "UPDATE connections SET status=?, updated_at=? WHERE provider=? AND handle=?",
                (status, time.time(), provider, handle),
            )
        return cur.rowcount > 0

    # -- OAuth state secret and pending handshakes --------------------------

    def state_secret(self) -> str:
        """The secret that signs OAuth ``state`` values.

        ``FABRI_OAUTH_STATE_SECRET`` wins when set, and then nothing is
        written. Otherwise one random secret is stored in ``service_secrets``
        on first use and read back from then on. Two processes racing on a
        fresh file both insert with ``INSERT OR IGNORE`` and both read the
        one row that won, so they sign with the same secret.
        """
        pinned = os.environ.get(STATE_SECRET_ENV, "")
        if pinned:
            return pinned
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO service_secrets(name, value, created_at) VALUES(?,?,?)",
                ("oauth_state", secrets.token_urlsafe(48), time.time()),
            )
            row = connection.execute(
                "SELECT value FROM service_secrets WHERE name='oauth_state'"
            ).fetchone()
        self._secure_sidecars()
        return row["value"]

    def pending_put(
        self,
        *,
        nonce: str,
        provider: str,
        handle: str | None,
        scope_kind: str,
        user_id: str | None,
        code_verifier: str,
        scopes: str,
        redirect_uri: str,
        now: float | None = None,
    ) -> None:
        """Record one OAuth handshake that is waiting for its callback. It
        lives PENDING_TTL_S seconds. Rows already past their expiry are
        removed on the way."""
        if scope_kind not in SCOPE_KINDS:
            raise ValueError("scope_kind must be 'org' or 'user'")
        if not nonce:
            raise ValueError("nonce must not be empty")
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("DELETE FROM pending_oauth WHERE expires_at < ?", (now,))
            connection.execute(
                """
                INSERT INTO pending_oauth(
                    nonce, provider, handle, scope_kind, user_id, code_verifier,
                    scopes, redirect_uri, created_at, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    nonce, provider, handle, scope_kind, user_id, code_verifier,
                    scopes, redirect_uri, now, now + PENDING_TTL_S,
                ),
            )
        self._secure_sidecars()

    def pending_pop(
        self,
        nonce: str,
        *,
        now: float | None = None,
        check: Callable[[dict], None] | None = None,
    ) -> dict | None:
        """Take the pending handshake for `nonce`, once. An expired row is
        deleted and None is returned.

        ``check`` is called with a live row inside the same write
        transaction, before the row is deleted. When it raises, the
        transaction rolls back, the row stays for its rightful owner and the
        exception propagates. That is how a callback that reaches the wrong
        caller is refused without spending the other caller's flow.
        """
        now = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT nonce, provider, handle, scope_kind, user_id, code_verifier, "
                    "scopes, redirect_uri, created_at, expires_at "
                    "FROM pending_oauth WHERE nonce=?",
                    (nonce,),
                ).fetchone()
                live = dict(row) if row is not None and row["expires_at"] >= now else None
                if live is not None and check is not None:
                    check(dict(live))
                if row is not None:
                    connection.execute("DELETE FROM pending_oauth WHERE nonce=?", (nonce,))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return live

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        result = dict(row)
        raw = result.get("meta")
        if raw is not None:
            try:
                result["meta"] = json.loads(raw)
            except ValueError:
                result["meta"] = None
        return result

    def get(self, provider: str, handle: str) -> dict | None:
        """The connection's row without any token column, or None."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM connections WHERE provider=? AND handle=?",
                (provider, handle),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list(
        self,
        provider: str | None = None,
        *,
        scope_kind: str | None = None,
        handle: str | None = None,
    ) -> list[dict]:
        """Rows without any token column, ordered by provider then handle."""
        clauses, params = [], []
        for column, value in (
            ("provider", provider), ("scope_kind", scope_kind), ("handle", handle)
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM connections{where} ORDER BY provider, handle",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def scope_kind(self, provider: str, handle: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT scope_kind FROM connections WHERE provider=? AND handle=?",
                (provider, handle),
            ).fetchone()
        return row["scope_kind"] if row is not None else None

    def get_token(
        self, provider: str, handle: str, *, now: float | None = None
    ) -> str | None:
        """The connection's access token, refreshed first when it is about to
        expire. None when there is no row, the row is revoked, or the token
        has expired and could not be refreshed."""
        now = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_token, refresh_token, expires_at, status FROM connections "
                "WHERE provider=? AND handle=?",
                (provider, handle),
            ).fetchone()
        if row is None or row["status"] == "revoked":
            return None
        if row["expires_at"] is None or row["expires_at"] - now > REFRESH_MARGIN_S:
            return row["access_token"]
        refresher = self.refreshers.get(provider)
        if refresher is None or not row["refresh_token"]:
            return row["access_token"] if row["expires_at"] > now else None
        return self._refresh(provider, handle, refresher, now)

    def _refresh(
        self, provider: str, handle: str, refresher: Refresher, now: float
    ) -> str | None:
        """Refresh one token without holding the write lock during the call.

        A short write transaction takes a lease on the row (it sets
        ``refresh_lease_until``) and reads the refresh token. The vendor call
        then runs with no transaction open. A second short transaction stores
        the result only when the stored refresh token is still the one that
        was spent, and drops the lease. Two servers sharing the database
        therefore spend a refresh token once, and a slow vendor never blocks
        other writes to the file.
        """
        state, row = self._claim_refresh(provider, handle, now)
        if state == "done":
            return row
        if state == "busy":
            return self._await_refresh(provider, handle, now, row)
        spent = row["refresh_token"]
        try:
            fresh = refresher(spent)
            access = fresh["access_token"]
            if not access:
                raise KeyError("access_token")
        except Exception as exc:  # noqa: BLE001 -- logged without the token
            self._release_lease(provider, handle, spent)
            logger.warning(
                "%s token refresh failed for a connection: %s", provider, type(exc).__name__,
            )
            return row["access_token"] if (row["expires_at"] or 0) > now else None
        expires_in = fresh.get("expires_in")
        with self._connect() as connection:
            cur = connection.execute(
                "UPDATE connections SET access_token=?, refresh_token=?, expires_at=?, "
                "updated_at=?, refresh_lease_until=NULL "
                "WHERE provider=? AND handle=? AND refresh_token=? AND status!='revoked'",
                (
                    access,
                    fresh.get("refresh_token") or spent,
                    now + float(expires_in) if expires_in else None,
                    now,
                    provider,
                    handle,
                    spent,
                ),
            )
        self._secure_sidecars()
        if cur.rowcount == 1:
            return access
        # The row was replaced, removed or revoked while the call ran. What is
        # stored now wins over the token that just came back.
        logger.warning(
            "%s token refresh result was dropped: the connection changed meanwhile", provider,
        )
        return self._live_token(provider, handle, now)

    def _claim_refresh(self, provider: str, handle: str, now: float) -> tuple[str, object]:
        """("done", token or None), ("busy", row) when another caller holds
        the lease, or ("claimed", row) once this caller holds it."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                found = connection.execute(
                    "SELECT access_token, refresh_token, expires_at, status, refresh_lease_until "
                    "FROM connections WHERE provider=? AND handle=?",
                    (provider, handle),
                ).fetchone()
                row = dict(found) if found is not None else None
                if row is None or row["status"] == "revoked":
                    connection.execute("ROLLBACK")
                    return "done", None
                if row["expires_at"] is None or row["expires_at"] - now > REFRESH_MARGIN_S:
                    connection.execute("ROLLBACK")
                    return "done", row["access_token"]
                if not row["refresh_token"]:
                    connection.execute("ROLLBACK")
                    return "done", row["access_token"] if row["expires_at"] > now else None
                lease = row["refresh_lease_until"]
                if lease is not None and lease > now:
                    connection.execute("ROLLBACK")
                    return "busy", row
                connection.execute(
                    "UPDATE connections SET refresh_lease_until=? WHERE provider=? AND handle=?",
                    (now + REFRESH_LEASE_S, provider, handle),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return "claimed", row

    def _release_lease(self, provider: str, handle: str, spent: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE connections SET refresh_lease_until=NULL "
                "WHERE provider=? AND handle=? AND refresh_token=?",
                (provider, handle, spent),
            )

    def _live_token(self, provider: str, handle: str, now: float) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_token, expires_at, status FROM connections "
                "WHERE provider=? AND handle=?",
                (provider, handle),
            ).fetchone()
        if row is None or row["status"] == "revoked":
            return None
        if row["expires_at"] is not None and row["expires_at"] <= now:
            return None
        return row["access_token"]

    def _await_refresh(self, provider: str, handle: str, now: float, row: dict) -> str | None:
        """Another caller is refreshing. A token that is still valid is used
        at once; an expired one waits up to REFRESH_WAIT_S for the result."""
        if (row["expires_at"] or 0) > now:
            return row["access_token"]
        spent = row["refresh_token"]
        deadline = time.monotonic() + REFRESH_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self._connect() as connection:
                current = connection.execute(
                    "SELECT refresh_token, refresh_lease_until FROM connections "
                    "WHERE provider=? AND handle=?",
                    (provider, handle),
                ).fetchone()
            if current is None:
                return None
            if current["refresh_lease_until"] is None or current["refresh_token"] != spent:
                return self._live_token(provider, handle, now)
        return None
