"""Resolve connector secret references without exposing secret values."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fabri.tools.credential_store import (
    ActorCredentialStore,
    CredentialNotFoundError,
    CredentialStore,
    CredentialStoreError,
    EnvCredentialStore,
    ScopedCredentialStore,
    SqliteInstallCredentialStore,
)


class MalformedCredentialRefError(CredentialStoreError, ValueError):
    """Raised when a secret reference is not exactly ``provider:handle``."""


_ACTOR_FROM_ENV = object()

# The install database a direct `fabri run` reads stored connections from
# (the one `fabri connect` writes). Held in this process only, never in
# os.environ, so no tool subprocess inherits the path.
_cli_install_db: Path | None = None


def is_service_child(env: Mapping[str, str] | None = None) -> bool:
    """True in a run `fabri serve` launched: it holds the credential broker
    env, or the FABRI_SESSION_ID the launcher always sets. Such a run must
    never open the install database."""
    from fabri.tools.credential_store import BROKER_ENV_NAMES

    e = os.environ if env is None else env
    return bool(e.get("FABRI_SESSION_ID")) or any(e.get(name) for name in BROKER_ENV_NAMES)


def use_cli_install_db(path: str | Path | None) -> None:
    """Point default_credential_store at the install database for this
    process, for a direct CLI run. Ignored in a service child, and when the
    file does not exist; None clears it."""
    global _cli_install_db
    if path is None or is_service_child():
        _cli_install_db = None
        return
    p = Path(path)
    _cli_install_db = p if p.is_file() else None


def default_credential_store(*, actor: str | None | object = _ACTOR_FROM_ENV) -> CredentialStore:
    """The install database when FABRI_INSTALL_DB is set, else the one a
    direct CLI run picked with use_cli_install_db (never in a service child),
    else env vars. FABRI_CRED_AUDIT_LOG names a JSONL file that records every
    lookup.

    The store resolves for one run actor: ``actor`` when given, else
    FABRI_RUN_ACTOR. A ``${actor}`` handle becomes that id, and a row stored
    with user scope resolves only for its own user (see
    ActorCredentialStore). The actor wrapper sits outside the audit wrapper,
    so an audit line records the substituted handle."""
    store: CredentialStore
    if p := os.environ.get("FABRI_INSTALL_DB"):
        store = SqliteInstallCredentialStore(Path(p))
    elif _cli_install_db is not None and not is_service_child():
        store = SqliteInstallCredentialStore(_cli_install_db)
    else:
        store = EnvCredentialStore()
    if audit := os.environ.get("FABRI_CRED_AUDIT_LOG"):
        store = ScopedCredentialStore(store, audit_path=audit)
    if actor is _ACTOR_FROM_ENV:
        actor = os.environ.get("FABRI_RUN_ACTOR") or None
    return ActorCredentialStore(store, actor if isinstance(actor, str) else None)


def resolve_secret(ref: str, store: CredentialStore | None = None) -> str:
    """Resolve a ``provider:handle`` reference through a credential store.

    Reference errors deliberately do not repeat the supplied reference because
    malformed input may itself contain sensitive material.
    """
    if not isinstance(ref, str) or ref.count(":") != 1:
        raise MalformedCredentialRefError(
            "Credential reference must have the form provider:handle"
        )

    provider, handle = ref.split(":", 1)
    if not provider or not handle:
        raise MalformedCredentialRefError(
            "Credential reference requires a non-empty provider and handle"
        )

    credential_store = store if store is not None else default_credential_store()
    return credential_store.get(provider, handle)


__all__ = [
    "CredentialNotFoundError",
    "MalformedCredentialRefError",
    "is_service_child",
    "resolve_secret",
    "use_cli_install_db",
]
