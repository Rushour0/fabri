"""Surface health: each adapter reports which env vars it is missing and how
many workspaces are installed, and GET /surfaces/health lists them. No
secret value appears in the report."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fabri.service.http_server import _Handler
from fabri.service.surfaces.github import GitHubAdapter
from fabri.service.surfaces.linear import LinearAdapter
from fabri.service.surfaces.registry import SurfaceRegistry
from fabri.service.surfaces.slack import SlackAdapter

pytestmark = pytest.mark.unit

ENV = ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN", "GITHUB_APP_WEBHOOK_SECRET", "FABRI_CRED_GITHUB_APP_ID",
       "FABRI_CRED_GITHUB_PRIVATE_KEY", "FABRI_CRED_GITHUB_DEFAULT", "LINEAR_WEBHOOK_SECRET",
       "FABRI_CRED_LINEAR_DEFAULT", "LINEAR_API_KEY")


class _Store:
    def __init__(self, n: int) -> None:
        self.n = n

    def list(self) -> list[dict]:
        return [{"id": i} for i in range(self.n)]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def _service(slack=0, github=0, linear=0):
    return SimpleNamespace(install_store=_Store(slack), github_install_store=_Store(github),
                           linear_install_store=_Store(linear))


def test_slack_missing_signing_secret_and_no_way_to_reply():
    health = SlackAdapter({}, _service()).health()
    assert health["ok"] is False and health["missing"] == ["SLACK_SIGNING_SECRET"]
    assert health["installs"] == 0
    assert health["issues"][1].startswith("no way to post results: set SLACK_BOT_TOKEN")


def test_slack_installs_count_as_a_way_to_reply(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig-value")
    health = SlackAdapter({}, _service(slack=2)).health()
    assert health == {"name": "slack", "webhook_path": "/slack/events", "ok": True,
                      "missing": [], "installs": 2, "issues": []}


def test_github_needs_both_app_vars_or_a_pat(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "hook-value")
    monkeypatch.setenv("FABRI_CRED_GITHUB_APP_ID", "123")
    assert GitHubAdapter(_service()).health()["ok"] is False
    monkeypatch.setenv("FABRI_CRED_GITHUB_PRIVATE_KEY", "pem-value")
    assert GitHubAdapter(_service()).health()["ok"] is True


def test_linear_env_key_is_enough(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "hook-value")
    monkeypatch.setenv("LINEAR_API_KEY", "lin-value")
    assert LinearAdapter(_service()).health()["ok"] is True


def test_route_lists_registered_surfaces_without_secret_values(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig-value")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-value")
    service = _service(linear=1)
    surfaces = SurfaceRegistry()
    surfaces.register(SlackAdapter({}, service))
    surfaces.register(LinearAdapter(service))
    service.surfaces, service.auth_enabled = surfaces, False

    handler = object.__new__(_Handler)
    handler.server = SimpleNamespace(service=service, serve_studio=False)
    handler.path = "/surfaces/health"
    sent: list[tuple[int, dict]] = []
    handler._send_json = lambda code, payload: sent.append((code, payload))
    handler.do_GET()

    [(code, body)] = sent
    assert code == 200
    by_name = {s["name"]: s for s in body["surfaces"]}
    assert by_name["slack"]["ok"] is True
    assert by_name["linear"]["missing"] == ["LINEAR_WEBHOOK_SECRET"] and by_name["linear"]["installs"] == 1
    assert "value" not in json.dumps(body)
