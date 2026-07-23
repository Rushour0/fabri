"""Focused tests for the shared SSRF guards and canonical fetch recipe."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from fabri.tools.security.ssrf import (
    ValidatingRedirect,
    host_is_blocked,
    validate_url,
)

pytestmark = pytest.mark.unit

RECIPE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "fabri"
    / "tools"
    / "recipes"
    / "fetch_url.py"
)


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {
        "localhost": "127.0.0.1",
        "public.example": "93.184.216.34",
    }

    def getaddrinfo(
        host: str, port: int | str | None
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        address = addresses.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.delenv("FABRI_FETCH_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param(
            "http://localhost/admin", "private/reserved", id="localhost"
        ),
        pytest.param(
            "http://127.0.0.1/admin", "private/reserved", id="loopback"
        ),
        pytest.param(
            "http://169.254.169.254/latest/meta-data/",
            "private/reserved",
            id="link-local",
        ),
        pytest.param("http://10.0.0.1/admin", "private/reserved", id="rfc1918"),
        pytest.param("file:///etc/passwd", r"only http\(s\)", id="file-scheme"),
        pytest.param(
            "ftp://public.example/file", r"only http\(s\)", id="ftp-scheme"
        ),
    ],
)
def test_validate_url_blocks_unsafe_targets(
    fake_dns: None, url: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_url(url)


def test_validate_url_allows_public_https_host(fake_dns: None) -> None:
    url = "https://public.example/path?query=yes"

    assert host_is_blocked("public.example") is False
    assert validate_url(url) == url


def test_private_host_override_keeps_original_semantics(
    fake_dns: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FABRI_FETCH_ALLOW_PRIVATE", "1")
    url = "http://127.0.0.1/admin"

    assert host_is_blocked("127.0.0.1") is False
    assert validate_url(url) == url


def test_redirect_handler_revalidates_target(fake_dns: None) -> None:
    handler = ValidatingRedirect()
    request = urllib.request.Request("https://public.example/start")

    with pytest.raises(ValueError, match="private/reserved"):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )


def test_recipe_import_delegates_to_shared_guards(fake_dns: None) -> None:
    from fabri.tools.recipes import fetch_url

    url = "https://public.example/path"
    assert fetch_url._host_is_blocked("public.example") is False
    assert fetch_url._validate(url) == url
    assert fetch_url._ValidatingRedirect is ValidatingRedirect


def test_recipe_runs_as_isolated_subprocess() -> None:
    process = subprocess.run(
        [sys.executable, "-I", str(RECIPE_PATH)],
        input=json.dumps({"url": "file:///etc/passwd"}),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert process.returncode == 1
    assert json.loads(process.stdout) == {
        "error": "refused: only http(s) supported, got 'file:///etc/passwd'"
    }
    assert process.stderr == ""
