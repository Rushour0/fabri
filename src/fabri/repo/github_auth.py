"""GitHub authentication providers for PATs and GitHub Apps."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fabri.tools.credential_store import CredentialStore
from fabri.tools.security.ssrf import ValidatingRedirect, validate_url

try:
    # This is the public seam expected by repo authentication.
    from fabri.tools.credential_store import resolve_secret
except ImportError:
    # Compatibility with trees where the resolver has not yet been re-exported.
    from fabri.tools.secret_refs import resolve_secret


_GITHUB_API = "https://api.github.com"
_TOKEN_REFRESH_SKEW_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 30
_REDACTED = "[REDACTED]"

_opener = urllib.request.build_opener(ValidatingRedirect)


class GitHubAuth(Protocol):
    """Provide a GitHub API token."""

    def get_token(self) -> str:
        """Return a usable GitHub token."""
        ...


class PatAuth:
    """Resolve a personal access token from the credential store."""

    def __init__(self, store: CredentialStore | None = None) -> None:
        self._store = store

    def get_token(self) -> str:
        """Return the configured default GitHub PAT."""
        return resolve_secret("github:default", self._store)


class AppAuth:
    """Mint and cache GitHub App installation access tokens."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        installation_id: str | None = None,
    ) -> None:
        self._store = store
        self._installation_id = installation_id
        self._cache: dict[str, tuple[str, float]] = {}  # keyed by installation id -> (token, expiry)

    def get_token(self) -> str:
        """Return a cached or newly minted installation access token."""
        now = time.time()
        if self._installation_id is not None:
            inst = self._installation_id
            cached = self._cache.get(inst)
            if (
                cached is not None
                and now < cached[1] - _TOKEN_REFRESH_SKEW_SECONDS
            ):
                return cached[0]
        else:
            inst = None
            cached = None

        sensitive_values = [cached[0]] if cached else []
        try:
            app_id = resolve_secret("github:app_id", self._store)
            if inst is None:
                inst = resolve_secret(
                    "github:installation_id",
                    self._store,
                )
                cached = self._cache.get(inst)
                if (
                    cached is not None
                    and now < cached[1] - _TOKEN_REFRESH_SKEW_SECONDS
                ):
                    return cached[0]
                if cached:
                    sensitive_values.append(cached[0])

            pem_path = resolve_secret("github:private_key", self._store)

            if not inst.isdecimal():
                raise ValueError("GitHub installation ID must be numeric")

            private_key = Path(pem_path).read_text(encoding="utf-8")
            jwt_token = _encode_app_jwt(app_id, private_key, now)
            sensitive_values.append(jwt_token)

            url = (
                f"{_GITHUB_API}/app/installations/"
                f"{inst}/access_tokens"
            )
            validate_url(url)
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                },
                method="POST",
            )

            with _opener.open(
                request,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                payload = json.load(response)

            if not isinstance(payload, dict):
                raise ValueError("GitHub token response was not a JSON object")

            token = payload.get("token")
            if isinstance(token, str) and token:
                sensitive_values.append(token)
            else:
                raise ValueError("GitHub token response did not include a token")

            expires_at = payload.get("expires_at")
            if not isinstance(expires_at, str) or not expires_at:
                raise ValueError(
                    "GitHub token response did not include an expiry"
                )
            expiry = _parse_expiry(expires_at)
            if expiry <= now:
                raise ValueError("GitHub returned an already-expired token")

            self._cache[inst] = (token, expiry)
            return token
        except Exception as exc:
            detail = _redact(str(exc), sensitive_values)
            raise RuntimeError(
                f"GitHub App authentication failed: {detail}"
            ) from None


def installation_id_for_repo(
    account_or_repo: str | None,
    store: CredentialStore | None = None,
) -> str | None:
    """Resolve a GitHub App installation id for an account or owner/repo from installs.db (read-only). None => caller falls back to env."""
    if not account_or_repo:
        return None

    db = os.environ.get("FABRI_INSTALL_DB")
    if not db or not Path(db).exists():
        return None

    owner = (
        account_or_repo.split("/")[0]
        if "/" in account_or_repo
        else account_or_repo
    )
    try:
        connection = sqlite3.connect(
            f"file:{db}?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.OperationalError:
        return None

    try:
        try:
            row = connection.execute(
                "SELECT installation_id FROM github_installs "
                "WHERE account_login = ? LIMIT 1",
                (owner,),
            ).fetchone()
            if row is not None:
                return row[0]

            rows = connection.execute(
                "SELECT installation_id, repos FROM github_installs"
            )
            for installation_id, repos in rows:
                if repos is None:
                    continue
                try:
                    repo_names = json.loads(repos)
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(repo_names, list)
                    and account_or_repo in repo_names
                ):
                    return installation_id
        except sqlite3.OperationalError:
            return None
        return None
    finally:
        connection.close()


def build_github_auth(
    store: CredentialStore | None = None,
    *,
    repo: str | None = None,
) -> GitHubAuth:
    """Select GitHub App authentication when App credentials are configured."""
    if os.environ.get("FABRI_CRED_GITHUB_APP_ID"):
        iid = installation_id_for_repo(repo, store) if repo else None
        return AppAuth(store, installation_id=iid)
    return PatAuth(store)


def _encode_app_jwt(app_id: str, private_key: str, now: float) -> str:
    try:
        import jwt
    except ImportError:
        raise RuntimeError(
            "PyJWT is required for GitHub App authentication; "
            "pip install fabri[repo-github]"
        ) from None

    encoded = jwt.encode(
        {
            "iat": int(now) - 60,
            "exp": int(now) + 540,
            "iss": app_id,
        },
        private_key,
        algorithm="RS256",
    )
    if isinstance(encoded, bytes):
        return encoded.decode("utf-8")
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("PyJWT returned an invalid encoded token")
    return encoded


def _parse_expiry(expires_at: str) -> float:
    normalized = (
        f"{expires_at[:-1]}+00:00" if expires_at.endswith("Z") else expires_at
    )
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("GitHub token expiry did not include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def _redact(detail: str, sensitive_values: list[str]) -> str:
    redacted = detail
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, _REDACTED)
    return redacted


__all__ = [
    "AppAuth",
    "GitHubAuth",
    "PatAuth",
    "build_github_auth",
    "installation_id_for_repo",
]
