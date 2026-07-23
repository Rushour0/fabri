"""Recipe: send a plain-text email through SMTP using stored credentials."""
from __future__ import annotations

import json
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

# The recipe is launched as a plain script by its JSON manifest. Add the src/
# root so direct execution can import the shared credential resolver.
if not __package__:
    for _candidate in Path(__file__).resolve().parents:
        if (_candidate / "fabri" / "tools" / "secret_refs.py").is_file():
            sys.path.insert(0, str(_candidate))
            break

from fabri.tools.secret_refs import (
    CredentialNotFoundError,
    MalformedCredentialRefError,
    resolve_secret,
)

DEFAULT_CREDENTIAL = "smtp:default"
DEFAULT_PORT = 587
DEFAULT_TIMEOUT_SECONDS = 30
FROM_ADDR_ENV = "FABRI_SMTP_FROM_ADDR"


class InputError(ValueError):
    """Raised when recipe input does not match the manifest contract."""


class CredentialFormatError(ValueError):
    """Raised when an SMTP credential is not the documented JSON object."""


def _failure(error: str) -> dict[str, object]:
    return {"ok": False, "error": error}


def _required_string(args: dict[str, object], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"`{key}` must be a non-empty string")
    return value


def _recipients(value: object) -> list[str]:
    if isinstance(value, str):
        recipients = [value]
    elif isinstance(value, list):
        recipients = value
    else:
        raise InputError("`to` must be a string or a list of strings")

    if not recipients or any(
        not isinstance(recipient, str) or not recipient for recipient in recipients
    ):
        raise InputError("`to` must contain at least one non-empty string")
    return recipients


def _credential_parts(secret: str) -> tuple[str, str]:
    try:
        value = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise CredentialFormatError(
            'SMTP credential must be JSON: {"user":"...","password":"..."}'
        ) from exc

    if not isinstance(value, dict):
        raise CredentialFormatError(
            'SMTP credential must be JSON: {"user":"...","password":"..."}'
        )
    user = value.get("user")
    password = value.get("password")
    if not isinstance(user, str) or not user:
        raise CredentialFormatError("SMTP credential requires a non-empty `user`")
    if not isinstance(password, str) or not password:
        raise CredentialFormatError("SMTP credential requires a non-empty `password`")
    return user, password


def _redact(message: str, password: str) -> str:
    return message.replace(password, "[REDACTED]")


def send_email(args: dict[str, object]) -> dict[str, object]:
    """Validate arguments, send one message, and return a tool-result envelope."""
    try:
        recipients = _recipients(args.get("to"))
        subject = _required_string(args, "subject")
        body = _required_string(args, "body")
        host = _required_string(args, "host")

        port_value = args.get("port", DEFAULT_PORT)
        if (
            isinstance(port_value, bool)
            or not isinstance(port_value, int)
            or not 1 <= port_value <= 65535
        ):
            raise InputError("`port` must be an integer from 1 to 65535")

        use_tls = args.get("use_tls", True)
        if not isinstance(use_tls, bool):
            raise InputError("`use_tls` must be a boolean")

        credential = args.get("credential", DEFAULT_CREDENTIAL)
        if not isinstance(credential, str) or not credential:
            raise InputError("`credential` must be a non-empty string")

        from_addr = args.get("from_addr")
        if from_addr is not None and (
            not isinstance(from_addr, str) or not from_addr
        ):
            raise InputError("`from_addr` must be a non-empty string")
    except InputError as exc:
        return _failure(f"{type(exc).__name__}: {exc}")

    try:
        user, password = _credential_parts(resolve_secret(credential))
    except (
        CredentialNotFoundError,
        MalformedCredentialRefError,
        CredentialFormatError,
    ) as exc:
        return _failure(f"{type(exc).__name__}: {exc}")

    sender = from_addr or os.environ.get(FROM_ADDR_ENV) or user
    message = EmailMessage()
    try:
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message_id = make_msgid()
        message["Message-ID"] = message_id
        message.set_content(body)
    except (TypeError, ValueError) as exc:
        return _failure(f"InputError: invalid email content ({type(exc).__name__})")

    try:
        with smtplib.SMTP(
            host, port_value, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as smtp:
            if use_tls:
                smtp.starttls()
            smtp.login(user, password)
            refused = smtp.send_message(
                message,
                from_addr=sender,
                to_addrs=recipients,
            )
    except smtplib.SMTPAuthenticationError as exc:
        return _failure(f"SMTP authentication failed (code {exc.smtp_code})")
    except (smtplib.SMTPException, OSError) as exc:
        detail = _redact(str(exc), password)
        return _failure(f"SMTP delivery failed: {detail}")
    except Exception as exc:
        detail = _redact(str(exc), password)
        return _failure(f"Unexpected SMTP failure ({type(exc).__name__}): {detail}")

    accepted = [recipient for recipient in recipients if recipient not in refused]
    return {
        "ok": True,
        "result": {
            "accepted": accepted,
            "message_id": message_id,
        },
    }


def main() -> int:
    try:
        raw_args = json.loads(sys.stdin.read())
        if not isinstance(raw_args, dict):
            raise InputError("input must be a JSON object")
        args: dict[str, object] = raw_args
        response = send_email(args)
    except (json.JSONDecodeError, InputError) as exc:
        response = _failure(f"{type(exc).__name__}: {exc}")

    print(json.dumps(response))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
