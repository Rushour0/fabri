"""Small stdlib-only user and signed-session primitives for Fabri Studio."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path


_DUMMY_SALT = b"fabri-auth-dummy"
_DUMMY_HASH = "00" * 32


class UserStore:
    """SQLite-backed email/password user store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    pw_hash TEXT,
                    pw_salt TEXT,
                    created REAL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _email(email: str) -> str:
        return email.strip().lower()

    def create_user(self, email: str, password: str) -> str:
        normalized = self._email(email)
        salt = os.urandom(16)
        password_hash = hashlib.scrypt(
            password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
        ).hex()
        user_id = uuid.uuid4().hex
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users (id, email, pw_hash, pw_salt, created) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, normalized, password_hash, salt.hex(), time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("email already registered") from exc
        return user_id

    def verify_user(self, email: str, password: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, pw_hash, pw_salt FROM users WHERE email = ?",
                (self._email(email),),
            ).fetchone()
        if row is None:
            # Keep unknown-email attempts comparable to a real password check.
            calculated = hashlib.scrypt(
                password.encode(),
                salt=_DUMMY_SALT,
                n=2**14,
                r=8,
                p=1,
                dklen=32,
            ).hex()
            hmac.compare_digest(calculated, _DUMMY_HASH)
            return None
        calculated = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(row[2]),
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        ).hex()
        return row[0] if hmac.compare_digest(calculated, row[1]) else None

    def get_user(self, user_id: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, email FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return {"id": row[0], "email": row[1]} if row is not None else None


def sign_session(user_id: str, secret: str, ttl_s: int) -> str:
    payload = f"{user_id}.{int(time.time()) + ttl_s}"
    signature = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_session(token: str, secret: str) -> str | None:
    try:
        user_id, expiry, signature = token.split(".")
        payload = f"{user_id}.{expiry}"
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if int(expiry) < time.time() or not hmac.compare_digest(signature, expected):
            return None
        return user_id
    except (AttributeError, TypeError, ValueError):
        return None


def build_set_cookie(token: str, max_age: int, secure: bool) -> str:
    cookie = f"fabri_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"
    return cookie + ("; Secure" if secure else "")


def build_clear_cookie(secure: bool) -> str:
    return build_set_cookie("", 0, secure)


def parse_cookie(cookie_header: str) -> dict[str, str]:
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    return {name: morsel.value for name, morsel in cookie.items()}
