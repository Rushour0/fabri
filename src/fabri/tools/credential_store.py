"""Credential storage abstractions for connector secrets.

The local environment-backed mapping is deterministic:

``provider:handle`` -> ``FABRI_CRED_<PROVIDER>_<HANDLE>``

Each component is uppercased and every non-ASCII-alphanumeric character is
replaced with ``_``. For example, ``slack:acme-eng`` maps to
``FABRI_CRED_SLACK_ACME_ENG``.
"""
from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

ENV_CREDENTIAL_PREFIX = "FABRI_CRED_"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


class CredentialStoreError(RuntimeError):
    """Base class for credential lookup failures."""


class CredentialNotFoundError(CredentialStoreError):
    """Raised when a credential handle has no value in the backing store."""


class CredentialScopeError(CredentialStoreError):
    """Raised when a handle is outside the grants of a ScopedCredentialStore."""


def credential_env_var(provider: str, handle: str) -> str:
    """Return the environment variable used for a provider and handle."""
    normalized_provider = _NON_ALNUM.sub("_", provider).upper()
    normalized_handle = _NON_ALNUM.sub("_", handle).upper()
    return f"{ENV_CREDENTIAL_PREFIX}{normalized_provider}_{normalized_handle}"


@runtime_checkable
class CredentialStore(Protocol):
    """Backend-independent lookup contract for connector credentials."""

    def get(self, provider: str, handle: str) -> str:
        """Return the secret for a parsed provider and handle."""
        ...


class EnvCredentialStore:
    """Read connector credentials from deterministically named env vars."""

    def get(self, provider: str, handle: str) -> str:
        env_var = credential_env_var(provider, handle)
        value = os.environ.get(env_var)
        if not value:
            raise CredentialNotFoundError(
                f"Credential is unavailable; set environment variable {env_var}"
            )
        return value

    def __repr__(self) -> str:
        return "EnvCredentialStore()"


class SqliteInstallCredentialStore:
    """Read per-workspace Slack/Linear credentials from the install database."""

    def __init__(self, db_path=None, fallback=None) -> None:
        self._db_path = db_path or os.environ.get("FABRI_INSTALL_DB")
        self._fallback = fallback or EnvCredentialStore()

    def get(self, provider: str, handle: str) -> str:
        if (
            provider == "slack"
            and handle != "default"
            and self._db_path
            and Path(self._db_path).exists()
        ):
            connection = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=30
            )
            try:
                row = connection.execute(
                    "SELECT bot_token FROM installs WHERE team_id=?", (handle,)
                ).fetchone()
            finally:
                connection.close()
            if row is not None and row[0]:
                return row[0]
            raise CredentialNotFoundError(
                "no Slack install for this workspace"
            )
        if provider == "linear" and handle != "default" and self._db_path and Path(self._db_path).exists():
            # Through the install store so a token near expiry is refreshed
            # (needs LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in this process).
            from fabri.service import linear_oauth
            from fabri.service.install_store import LinearInstallStore

            token = LinearInstallStore(
                Path(self._db_path), refresher=linear_oauth.refresh_access_token
            ).get_token(handle)
            if token:
                return token
            raise CredentialNotFoundError(
                "no Linear install for this workspace"
            )
        return self._fallback.get(provider, handle)

    def __repr__(self) -> str:
        return "SqliteInstallCredentialStore()"


class ScopedCredentialStore:
    """Wrap a store so it only resolves granted handles, and log every lookup.

    `grants` lists ``provider:handle`` strings or fnmatch patterns such as
    ``provider:*`` for every handle of one provider. A host builds one of these per run with the
    tenant's own handles, so a run for workspace A cannot read workspace B's
    token even when both live in the same backing store. ``grants=None``
    allows every handle and only audits.

    With `audit_path`, each lookup appends one JSON line: time, actor,
    provider, handle and result (``ok``, ``denied`` or ``missing``). The
    secret value is never written.
    """

    def __init__(
        self,
        store: CredentialStore,
        grants: Iterable[str] | None = None,
        *,
        audit_path: str | Path | None = None,
        actor: str | None = None,
    ) -> None:
        self._store = store
        self._grants = None if grants is None else frozenset(grants)
        self._audit_path = Path(audit_path) if audit_path else None
        self._actor = actor

    def allows(self, provider: str, handle: str) -> bool:
        if self._grants is None:
            return True
        ref = f"{provider}:{handle}"
        return any(fnmatch.fnmatchcase(ref, grant) for grant in self._grants)

    def get(self, provider: str, handle: str) -> str:
        if not self.allows(provider, handle):
            self._audit(provider, handle, "denied")
            raise CredentialScopeError(
                f"credential {provider}:{handle} is not granted to this run"
            )
        try:
            value = self._store.get(provider, handle)
        except CredentialStoreError:
            self._audit(provider, handle, "missing")
            raise
        self._audit(provider, handle, "ok")
        return value

    def _audit(self, provider: str, handle: str, result: str) -> None:
        if self._audit_path is None:
            return
        line = json.dumps({"ts": round(time.time(), 3), "actor": self._actor, "provider": provider,
                           "handle": handle, "result": result}, sort_keys=True)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line + "\n")

    def __repr__(self) -> str:
        grants = "all" if self._grants is None else len(self._grants)
        return f"ScopedCredentialStore({self._store!r}, grants={grants})"
