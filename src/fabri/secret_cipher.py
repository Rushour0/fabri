"""Optional encryption at rest for stored secrets.

Connection tokens, refresh tokens, PKCE verifiers and the OAuth state secret
live in ``installs.db``. Without a key they are stored as plaintext in a 0600
file, as before. With a key they are stored as Fernet tokens (AES-128-CBC with
an HMAC-SHA256 tag, from the ``cryptography`` package) behind a version
prefix, so a reader can tell an encrypted value from an old plaintext one.

Key sources, first match wins:

* ``FABRI_SECRET_KEY``: one Fernet key, or several separated by commas. The
  first key encrypts; every key is tried when decrypting, so a key can be
  rotated by putting the new one first and running ``fabri connections rekey``.
* ``FABRI_SECRET_KEY_FILE``: a path to a file holding the same text.

A stored value that starts with the prefix is always treated as encrypted. A
plaintext token that happens to start with ``fabri-enc:v1:`` therefore cannot
be read back; no real vendor token has that shape.

``fabri connections keygen`` prints a new key. A key is never logged and never
appears in an error message.

Public API, kept small so other stores can adopt it:

* ``load_cipher(env=None) -> SecretCipher | None``
* ``SecretCipher.encrypt(text) -> str`` and ``SecretCipher.decrypt(value) -> str``
* ``encrypt_secret(value, cipher)`` and ``decrypt_secret(value, cipher)``,
  which pass None through and accept ``cipher=None``
* ``is_encrypted(value) -> bool``
* ``generate_key() -> str``
* ``SecretKeyError``: a malformed key, a wrong key, a missing ``cryptography``
  package, or an encrypted value with no key configured
* ``require_encryption(reason)``, ``plaintext_refusal(cipher)`` and
  ``PlaintextRefused``: the hosted-install guard (see below)

The hosted-install guard: ``fabri serve`` bound to a non-loopback address, or
with a non-loopback ``FABRI_PUBLIC_BASE_URL``, refuses to start without a key
unless ``FABRI_ALLOW_PLAINTEXT_SECRETS=1`` is set, and calls
``require_encryption``. From then on a store asks ``plaintext_refusal(cipher)``
before every secret write and refuses when no key is configured.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fabri.tools.credential_store import CredentialStoreError

SECRET_KEY_ENV = "FABRI_SECRET_KEY"
SECRET_KEY_FILE_ENV = "FABRI_SECRET_KEY_FILE"
ALLOW_PLAINTEXT_ENV = "FABRI_ALLOW_PLAINTEXT_SECRETS"
# Every encrypted value starts with this. A value without it is plaintext.
PREFIX = "fabri-enc:v1:"
INSTALL_HINT = 'install the extra with: pip install "fabri[encryption]"'

_REQUIRED_REASON: str | None = None


class SecretKeyError(CredentialStoreError):
    """The secret key is malformed, wrong, or unusable. The message never
    carries the key or a secret."""


class PlaintextRefused(ValueError):
    """A new token would be stored as plaintext on an install that requires a key."""


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _fernet_module():
    try:
        from cryptography import fernet
    except ImportError:
        raise SecretKeyError(
            f"{SECRET_KEY_ENV} is set but the cryptography package is not installed; {INSTALL_HINT}"
        ) from None
    return fernet


def generate_key() -> str:
    """A new random key in the format ``FABRI_SECRET_KEY`` takes."""
    return _fernet_module().Fernet.generate_key().decode("ascii")


class SecretCipher:
    """Encrypt and decrypt secret strings with one or more Fernet keys."""

    def __init__(self, keys: str | list[str]) -> None:
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",")]
        keys = [k for k in keys if k]
        if not keys:
            raise SecretKeyError(f"{SECRET_KEY_ENV} is empty")
        fernet = _fernet_module()
        built = []
        for index, key in enumerate(keys, start=1):
            try:
                built.append(fernet.Fernet(key.encode("ascii")))
            except (ValueError, TypeError, UnicodeEncodeError):
                raise SecretKeyError(
                    f"{SECRET_KEY_ENV} key {index} is malformed; it must be 32 url-safe base64 bytes "
                    "(run `fabri connections keygen` for a new one)"
                ) from None
        self._invalid = fernet.InvalidToken
        self._primary = built[0]
        self._fernet = fernet.MultiFernet(built)
        self.key_count = len(built)

    def __repr__(self) -> str:
        return f"SecretCipher(keys={self.key_count})"

    def encrypt(self, text: str) -> str:
        return PREFIX + self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """The plaintext of an encrypted value. A plaintext value (no prefix)
        is returned unchanged, which is how old rows stay readable."""
        if not is_encrypted(value):
            return value
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        except (self._invalid, ValueError, UnicodeError):
            raise SecretKeyError(
                f"a stored secret cannot be decrypted with {SECRET_KEY_ENV}; the key is wrong or the "
                "value is damaged"
            ) from None

    def needs_rewrite(self, value: object) -> bool:
        """True for a plaintext value, or one not encrypted with the first key."""
        if not isinstance(value, str) or not value:
            return False
        if not is_encrypted(value):
            return True
        try:
            self._primary.decrypt(value[len(PREFIX):].encode("ascii"))
        except (self._invalid, UnicodeError):
            return True
        return False


def _key_text(env: Mapping[str, str]) -> str | None:
    text = env.get(SECRET_KEY_ENV, "")
    if text.strip():
        return text.strip()
    path = env.get(SECRET_KEY_FILE_ENV, "")
    if not path:
        return None
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except UnicodeError:
        raise SecretKeyError(f"{SECRET_KEY_FILE_ENV} is not a UTF-8 text file") from None
    except OSError as exc:
        raise SecretKeyError(f"{SECRET_KEY_FILE_ENV} cannot be read: {type(exc).__name__}") from None
    if not text:
        raise SecretKeyError(f"{SECRET_KEY_FILE_ENV} names an empty file")
    return text


def load_cipher(env: Mapping[str, str] | None = None) -> SecretCipher | None:
    """The cipher the environment configures, or None when no key is set.
    Raises SecretKeyError for a key that cannot be used, so a bad key fails
    closed instead of falling back to plaintext."""
    text = _key_text(os.environ if env is None else env)
    return SecretCipher(text) if text else None


def encrypt_secret(value: str | None, cipher: SecretCipher | None) -> str | None:
    """``value`` encrypted when there is a cipher, else unchanged. None stays None."""
    if value is None or cipher is None:
        return value
    return cipher.encrypt(value)


def decrypt_secret(value: str | None, cipher: SecretCipher | None) -> str | None:
    """The plaintext of a stored value. Plaintext and None pass through.
    An encrypted value with no cipher raises SecretKeyError."""
    if not is_encrypted(value):
        return value
    if cipher is None:
        raise SecretKeyError(f"a stored secret is encrypted; set {SECRET_KEY_ENV} to read it")
    return cipher.decrypt(value)


# -- hosted-install guard ---------------------------------------------------


def require_encryption(reason: str | None) -> None:
    """Make new token writes in this process need a key. None turns it off."""
    global _REQUIRED_REASON
    _REQUIRED_REASON = reason


def encryption_required() -> str | None:
    return _REQUIRED_REASON


def plaintext_refusal(
    cipher: SecretCipher | None, env: Mapping[str, str] | None = None
) -> str | None:
    """The reason a new token may not be stored as plaintext, or None when it may."""
    if cipher is not None or _REQUIRED_REASON is None:
        return None
    source = os.environ if env is None else env
    if source.get(ALLOW_PLAINTEXT_ENV, "").strip().lower() in ("1", "true", "yes"):
        return None
    return (
        f"{_REQUIRED_REASON}, so new tokens are only stored encrypted; set {SECRET_KEY_ENV} "
        f"(see `fabri connections keygen`), or set {ALLOW_PLAINTEXT_ENV}=1 to store them as plaintext"
    )


__all__ = [
    "ALLOW_PLAINTEXT_ENV", "PREFIX", "SECRET_KEY_ENV", "SECRET_KEY_FILE_ENV", "PlaintextRefused",
    "SecretCipher", "SecretKeyError", "decrypt_secret", "encrypt_secret", "encryption_required",
    "generate_key", "is_encrypted", "load_cipher", "plaintext_refusal", "require_encryption",
]
