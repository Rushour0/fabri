from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from io import StringIO
from typing import Callable

import pytest

from fabri.tools.recipes import send_email

pytestmark = pytest.mark.unit


class RecordingSMTP:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: int,
        *,
        refused: dict[str, tuple[int, bytes]] | None = None,
        login_error: BaseException | None = None,
        tls_error: BaseException | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.refused = refused or {}
        self.login_error = login_error
        self.tls_error = tls_error
        self.calls: list[str] = []
        self.user: str | None = None
        self.password: str | None = None
        self.message: EmailMessage | None = None
        self.from_addr: str | None = None
        self.to_addrs: list[str] | None = None

    def __enter__(self) -> RecordingSMTP:
        self.calls.append("enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.calls.append("exit")

    def starttls(self) -> None:
        self.calls.append("starttls")
        if self.tls_error is not None:
            raise self.tls_error

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.user = user
        self.password = password
        if self.login_error is not None:
            raise self.login_error

    def send_message(
        self,
        message: EmailMessage,
        *,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, tuple[int, bytes]]:
        self.calls.append("send_message")
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        return self.refused


def _valid_args(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "to": "recipient@example.com",
        "subject": "Build complete",
        "body": "The build completed successfully.",
        "host": "smtp.example.com",
    }
    args.update(overrides)
    return args


def _install_smtp(
    monkeypatch: pytest.MonkeyPatch,
    **smtp_options: object,
) -> list[RecordingSMTP]:
    instances: list[RecordingSMTP] = []

    def factory(host: str, port: int, timeout: int) -> RecordingSMTP:
        smtp = RecordingSMTP(host, port, timeout, **smtp_options)
        instances.append(smtp)
        return smtp

    monkeypatch.setattr(send_email.smtplib, "SMTP", factory)
    return instances


def test_happy_path_uses_starttls_and_env_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "smtp-password"
    monkeypatch.setenv(
        "FABRI_CRED_SMTP_DEFAULT",
        json.dumps({"user": "mailer@example.com", "password": password}),
    )
    monkeypatch.setenv("FABRI_SMTP_FROM_ADDR", "robot@example.com")
    instances = _install_smtp(monkeypatch)

    response = send_email.send_email(_valid_args())

    assert response["ok"] is True
    result = response["result"]
    assert isinstance(result, dict)
    assert result["accepted"] == ["recipient@example.com"]
    assert isinstance(result["message_id"], str)
    assert result["message_id"].startswith("<")
    smtp = instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == (
        "smtp.example.com",
        587,
        30,
    )
    assert smtp.calls == ["enter", "starttls", "login", "send_message", "exit"]
    assert (smtp.user, smtp.password) == ("mailer@example.com", password)
    assert smtp.from_addr == "robot@example.com"
    assert smtp.to_addrs == ["recipient@example.com"]
    assert smtp.message is not None
    assert smtp.message.get_content_type() == "text/plain"
    assert smtp.message.get_content() == "The build completed successfully.\n"


def test_multiple_recipients_are_passed_and_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipients = ["one@example.com", "two@example.com"]
    monkeypatch.setenv(
        "FABRI_CRED_SMTP_DEFAULT",
        json.dumps({"user": "mailer@example.com", "password": "pw"}),
    )
    instances = _install_smtp(monkeypatch)

    response = send_email.send_email(
        _valid_args(to=recipients, from_addr="sender@example.com", use_tls=False)
    )

    assert response == {
        "ok": True,
        "result": {
            "accepted": recipients,
            "message_id": response["result"]["message_id"],
        },
    }
    smtp = instances[0]
    assert smtp.calls == ["enter", "login", "send_message", "exit"]
    assert smtp.to_addrs == recipients
    assert smtp.message is not None
    assert smtp.message["To"] == "one@example.com, two@example.com"


def test_missing_credential_returns_typed_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRI_CRED_SMTP_DEFAULT", raising=False)

    response = send_email.send_email(_valid_args())

    assert response["ok"] is False
    assert "CredentialNotFoundError" in response["error"]
    assert "FABRI_CRED_SMTP_DEFAULT" in response["error"]
    assert "password" not in response["error"].lower()


def test_auth_failure_is_readable_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "never-show-auth-secret"
    monkeypatch.setenv(
        "FABRI_CRED_SMTP_DEFAULT",
        json.dumps({"user": "mailer@example.com", "password": password}),
    )
    auth_error = smtplib.SMTPAuthenticationError(
        535,
        f"authentication failed for {password}".encode(),
    )
    _install_smtp(monkeypatch, login_error=auth_error)

    response = send_email.send_email(_valid_args())

    assert response == {
        "ok": False,
        "error": "SMTP authentication failed (code 535)",
    }
    assert password not in json.dumps(response)


def test_smtp_error_redacts_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "never-show-delivery-secret"
    monkeypatch.setenv(
        "FABRI_CRED_SMTP_DEFAULT",
        json.dumps({"user": "mailer@example.com", "password": password}),
    )
    _install_smtp(
        monkeypatch,
        tls_error=smtplib.SMTPException(f"server echoed {password}"),
    )

    response = send_email.send_email(_valid_args())

    assert response["ok"] is False
    assert response["error"] == "SMTP delivery failed: server echoed [REDACTED]"
    assert password not in json.dumps(response)


def test_main_writes_one_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "FABRI_CRED_SMTP_DEFAULT",
        json.dumps({"user": "mailer@example.com", "password": "pw"}),
    )
    _install_smtp(monkeypatch)
    monkeypatch.setattr(
        send_email.sys,
        "stdin",
        StringIO(json.dumps(_valid_args())),
    )

    exit_code = send_email.main()

    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert exit_code == 0
    assert response["ok"] is True
    assert response["result"]["accepted"] == ["recipient@example.com"]
    assert captured.err == ""
