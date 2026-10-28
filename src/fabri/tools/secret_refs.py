"""Resolve connector secret references without exposing secret values."""
from __future__ import annotations

import os
from pathlib import Path

from fabri.tools.credential_store import (
    CredentialNotFoundError,
    CredentialStore,
    CredentialStoreError,
    EnvCredentialStore,
    ScopedCredentialStore,
    SqliteInstallCredentialStore,
)


class MalformedCredentialRefError(CredentialStoreError, ValueError):
    """Raised when a secret reference is not exactly ``provider:handle``."""


def default_credential_store() -> CredentialStore:
    """The install database when FABRI_INSTALL_DB is set, else env vars.
    FABRI_CRED_AUDIT_LOG names a JSONL file that records every lookup."""
    store: CredentialStore
    if p := os.environ.get("FABRI_INSTALL_DB"):
        store = SqliteInstallCredentialStore(Path(p))
    else:
        store = EnvCredentialStore()
    if audit := os.environ.get("FABRI_CRED_AUDIT_LOG"):
        store = ScopedCredentialStore(store, audit_path=audit)
    return store


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
    "resolve_secret",
]
