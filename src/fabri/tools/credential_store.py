"""Credential storage abstractions for connector secrets.

The local environment-backed mapping is deterministic:

``provider:handle`` -> ``FABRI_CRED_<PROVIDER>_<HANDLE>``

Each component is uppercased and every non-ASCII-alphanumeric character is
replaced with ``_``. For example, ``slack:acme-eng`` maps to
``FABRI_CRED_SLACK_ACME_ENG``.
"""
from __future__ import annotations

import os
import re
from typing import Protocol, runtime_checkable

ENV_CREDENTIAL_PREFIX = "FABRI_CRED_"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


class CredentialStoreError(RuntimeError):
    """Base class for credential lookup failures."""


class CredentialNotFoundError(CredentialStoreError):
    """Raised when a credential handle has no value in the backing store."""


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
