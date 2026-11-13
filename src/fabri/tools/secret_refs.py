"""Resolve connector secret references without exposing secret values."""
from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator, Mapping
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
# (the one `fabri connect` writes). Held per run in a context variable, never
# in os.environ, so no tool subprocess inherits the path, and two runs in one
# process (each in its own thread or context) each see only their own.
# Worker threads that resolve credentials for a run must run in a copy of the
# run's context (contextvars.copy_context), as the agent's parallel tool pool
# does.
_cli_install_db: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "fabri_cli_install_db", default=None
)


def is_service_child(env: Mapping[str, str] | None = None) -> bool:
    """True in a run `fabri serve` launched: it holds the credential broker
    env, or the FABRI_SESSION_ID the launcher always sets. Such a run must
    never open the install database."""
    from fabri.tools.credential_store import BROKER_ENV_NAMES

    e = os.environ if env is None else env
    return bool(e.get("FABRI_SESSION_ID")) or any(e.get(name) for name in BROKER_ENV_NAMES)


def _usable_install_db(path: str | Path | None) -> Path | None:
    if path is None or is_service_child():
        return None
    p = Path(path)
    return p if p.is_file() else None


def use_cli_install_db(path: str | Path | None) -> contextvars.Token:
    """Point default_credential_store at the install database for the current
    context, for a direct CLI run. Ignored in a service child, and when the
    file does not exist; None clears it. Returns the token that
    reset_cli_install_db takes to restore the previous value."""
    return _cli_install_db.set(_usable_install_db(path))


def reset_cli_install_db(token: contextvars.Token) -> None:
    _cli_install_db.reset(token)


@contextlib.contextmanager
def cli_install_db(path: str | Path | None) -> Iterator[Path | None]:
    """use_cli_install_db for the length of a with block; the previous value
    comes back on exit, so an in-process caller does not keep the path."""
    token = use_cli_install_db(path)
    try:
        yield _cli_install_db.get()
    finally:
        _cli_install_db.reset(token)


def default_credential_store(*, actor: str | None | object = _ACTOR_FROM_ENV) -> CredentialStore:
    """Env vars in a service child (a run `fabri serve` launched), which
    never opens an install database, not even one FABRI_INSTALL_DB names.
    Otherwise the install database when FABRI_INSTALL_DB is set, else the one
    the current run picked with use_cli_install_db, else env vars. FABRI_CRED_AUDIT_LOG names a JSONL file that records every
    lookup.

    The store resolves for one run actor: ``actor`` when given, else
    FABRI_RUN_ACTOR. A ``${actor}`` handle becomes that id, and a row stored
    with user scope resolves only for its own user (see
    ActorCredentialStore). The actor wrapper sits outside the audit wrapper,
    so an audit line records the substituted handle."""
    store: CredentialStore
    cli_db = _cli_install_db.get()
    if is_service_child():
        store = EnvCredentialStore()
    elif p := os.environ.get("FABRI_INSTALL_DB"):
        store = SqliteInstallCredentialStore(Path(p))
    elif cli_db is not None:
        store = SqliteInstallCredentialStore(cli_db)
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

    from fabri.tools.credential_store import BROKER_ENV_NAMES, credential_env_var

    if credential_env_var(provider, handle) in BROKER_ENV_NAMES:
        raise MalformedCredentialRefError("Credential reference names the credential broker, which is not a secret")
    credential_store = store if store is not None else default_credential_store()
    return credential_store.get(provider, handle)


__all__ = [
    "CredentialNotFoundError",
    "MalformedCredentialRefError",
    "cli_install_db",
    "is_service_child",
    "reset_cli_install_db",
    "resolve_secret",
    "use_cli_install_db",
]
