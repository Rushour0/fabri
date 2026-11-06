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
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

ENV_CREDENTIAL_PREFIX = "FABRI_CRED_"
# Shares the prefix but holds a log file path, not a secret, so it is never
# stripped from a child's environment.
CREDENTIAL_AUDIT_LOG_ENV = "FABRI_CRED_AUDIT_LOG"
# The two variables a run gets when the service holds its credentials in a
# broker: the ask socket path and the capability key. They share the
# FABRI_CRED_ prefix on purpose, so the default strip below removes them from
# every tool subprocess. Only a sub-agent (or a nested run) is meant to keep them.
BROKER_SOCKET_ENV = "FABRI_CRED_BROKER_SOCKET"
BROKER_KEY_ENV = "FABRI_CRED_BROKER_KEY"
BROKER_ENV_NAMES = frozenset({BROKER_SOCKET_ENV, BROKER_KEY_ENV})
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


def credential_env_names(refs: Iterable[str], *, actor: str | None = None) -> frozenset[str]:
    """The ``FABRI_CRED_*`` names a list of ``provider:handle`` refs maps to.

    A ``${actor}`` handle becomes ``actor`` and is dropped when there is none.
    Malformed refs are dropped.
    """
    names = set()
    for ref in refs:
        if not isinstance(ref, str) or ref.count(":") != 1:
            continue
        provider, handle = ref.split(":", 1)
        if handle == "${actor}":
            if not actor:
                continue
            handle = actor
        if provider and handle:
            names.add(credential_env_var(provider, handle))
    return frozenset(names)


def wildcard_credential_names(
    patterns: Iterable[str], granted_refs: Iterable[tuple[str, str]]
) -> frozenset[str]:
    """The ``FABRI_CRED_*`` names a manifest's ``provider:*`` patterns reach.

    The names come from the run's granted ``(provider, handle)`` refs, and a
    pattern matches a ref only when the provider is exactly equal, so
    ``slack:*`` does not reach ``slack_bot``. A pattern that is not a
    ``provider:*`` wildcard is ignored here, and the broker names are never
    returned, so ``broker:*`` reaches nothing.
    """
    providers = set()
    for pattern in patterns:
        if isinstance(pattern, str) and pattern.count(":") == 1:
            provider, handle = pattern.split(":", 1)
            if provider and handle == "*":
                providers.add(provider)
    names = {
        credential_env_var(provider, handle)
        for provider, handle in granted_refs
        if provider in providers
    }
    return frozenset(names - BROKER_ENV_NAMES)


def env_without_credentials(
    env: Mapping[str, str] | None = None,
    *,
    keep: Iterable[str] = (),
    keep_broker: bool = False,
) -> dict[str, str]:
    """A copy of ``env`` (default: this process's) with every ``FABRI_CRED_*``
    variable removed, except the exact names in ``keep``.

    ``keep_broker=True`` also keeps the two broker names. Only the sub-agent
    and nested-run paths set it; a manifest tool, ``bash`` and ``python_exec``
    never do.

    Every subprocess a run starts gets its environment through this, so a tool
    the model drives cannot print a token the run holds for a source.
    """
    base = os.environ if env is None else env
    keep = frozenset(keep)
    if keep_broker:
        keep = keep | BROKER_ENV_NAMES
    return {
        key: value
        for key, value in base.items()
        if not key.startswith(ENV_CREDENTIAL_PREFIX) or key in keep or key == CREDENTIAL_AUDIT_LOG_ENV
    }


def broker_mode(env: Mapping[str, str] | None = None) -> bool:
    """True when both broker variables are set, so this process talks to a broker."""
    source = os.environ if env is None else env
    return all(source.get(name) for name in BROKER_ENV_NAMES)


def subagent_credential_names(config_path: str | Path, *, actor: str | None = None) -> frozenset[str]:
    """The ``FABRI_CRED_*`` names a sub-agent may receive.

    Under a broker (both broker variables set) that is exactly the two broker
    names, always. The broker answers from the parent run's own binding, so
    nothing is derived from the child config, which a model may have chosen.
    Without a broker it is the refs the child's ``tools.sources`` declare that
    its ``tools.policy`` grants, read the same way the service reads a run's
    bound config. An unreadable config or policy gives an empty set."""
    if broker_mode():
        return BROKER_ENV_NAMES
    from fabri.service.launcher import _load_bound_config, granted_secret_refs

    refs = granted_secret_refs(_load_bound_config(config_path), actor=actor)
    return frozenset(credential_env_var(provider, handle) for provider, handle in refs)


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

    def scope_kind(self, provider: str, handle: str) -> str | None:
        """Environment variables are set by the operator and carry no user scope."""
        return None

    def __repr__(self) -> str:
        return "EnvCredentialStore()"


class SqliteInstallCredentialStore:
    """Read stored connections (any provider) from the install database.

    A handle other than ``default`` is looked up in the ``connections`` table,
    so Slack, Linear, Cakemail and every later provider resolve the same way.
    A token near expiry is refreshed on the way out. No row, or a token that
    is dead, falls back to the environment store. If that has nothing either,
    the error names the fix and the environment variable, never the token.
    """

    def __init__(self, db_path=None, fallback=None) -> None:
        self._db_path = db_path or os.environ.get("FABRI_INSTALL_DB")
        self._fallback = fallback or EnvCredentialStore()

    def _has_db(self, handle: str) -> bool:
        return bool(handle != "default" and self._db_path and Path(self._db_path).exists())

    def _connections(self):
        # Lazy imports: the service package imports this module's siblings.
        from fabri.service.connection_store import ConnectionStore, default_refreshers

        return ConnectionStore(Path(self._db_path), refreshers=default_refreshers())

    def get(self, provider: str, handle: str) -> str:
        if not self._has_db(handle):
            return self._fallback.get(provider, handle)
        token = self._connections().get_token(provider, handle)
        if token:
            return token
        try:
            return self._fallback.get(provider, handle)
        except CredentialNotFoundError:
            raise CredentialNotFoundError(
                f"no {provider} connection for this handle; run `fabri connect {provider}` "
                f"or set {credential_env_var(provider, handle)}"
            ) from None

    def scope_kind(self, provider: str, handle: str) -> str | None:
        """Return ``org`` or ``user`` for a stored connection, None when there is no row or no db."""
        if not self._has_db(handle):
            return None
        return self._connections().scope_kind(provider, handle)

    def __repr__(self) -> str:
        return "SqliteInstallCredentialStore()"


ACTOR_SENTINEL = "${actor}"


class ActorCredentialStore:
    """Wrap a store so it resolves for one run actor.

    A ``${actor}`` handle becomes the actor's id, so ``gmail:${actor}`` reads
    the signed-in user's own row. A run with no actor gets a
    CredentialScopeError for such a handle. Any other handle is checked
    against the wrapped store's ``scope_kind``: a row stored with user scope
    resolves only when the handle is this actor's own id, so a run without
    an actor never receives a user-scoped row. A store with no
    ``scope_kind`` (environment variables, a host's own store) has no user
    rows and passes literal handles through. A store whose ``scope_kind``
    fails is treated as unknown and refused, since a user row could not be
    told apart from an org row.

    Wrapping an ActorCredentialStore again replaces its actor rather than
    stacking a second check. Messages name the provider only, never a handle
    or a value.
    """

    def __init__(self, store: CredentialStore, actor: str | None) -> None:
        if isinstance(store, ActorCredentialStore):
            store = store._store
        self._store = store
        self._actor = actor or None

    @property
    def actor(self) -> str | None:
        return self._actor

    @property
    def inner(self) -> CredentialStore:
        """The wrapped store."""
        return self._store

    def _handle(self, provider: str, handle: str) -> str:
        if handle == ACTOR_SENTINEL:
            if not self._actor:
                raise CredentialScopeError(
                    f"credential {provider}:{ACTOR_SENTINEL} needs an actor; this run has none"
                )
            return self._actor
        return handle

    def get(self, provider: str, handle: str) -> str:
        handle = self._handle(provider, handle)
        scope_kind = getattr(self._store, "scope_kind", None)
        if callable(scope_kind):
            try:
                kind = scope_kind(provider, handle)
            except Exception:  # noqa: BLE001 -- an unknown scope must not pass a user row
                raise CredentialScopeError(
                    f"credential {provider} has an unknown scope; it is not used for this run"
                ) from None
            if kind == "user" and handle != self._actor:
                raise CredentialScopeError(f"credential {provider} belongs to another user")
        return self._store.get(provider, handle)

    def scope_kind(self, provider: str, handle: str) -> str | None:
        """The wrapped store's scope for the handle after substitution. Raises
        AttributeError when the wrapped store cannot say."""
        inner = getattr(self._store, "scope_kind", None)
        if not callable(inner):
            raise AttributeError("the wrapped credential store does not report scope_kind")
        return inner(provider, self._handle(provider, handle))

    def __repr__(self) -> str:
        return f"ActorCredentialStore({self._store!r}, actor={'set' if self._actor else 'none'})"


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

    def scope_kind(self, provider: str, handle: str) -> str | None:
        """The wrapped store's scope for a row. Raises AttributeError when the
        wrapped store cannot say, so a caller that checks user rows fails closed
        instead of treating an unknown scope as org."""
        inner = getattr(self._store, "scope_kind", None)
        if not callable(inner):
            raise AttributeError("the wrapped credential store does not report scope_kind")
        return inner(provider, handle)

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
