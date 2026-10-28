"""`fabri installs list|remove` works on the server's install database
offline and never prints a token."""
from __future__ import annotations

import json
import sys

import pytest

from fabri.cli import main
from fabri.service.install_store import GitHubInstallStore, LinearInstallStore, SlackInstallStore

pytestmark = pytest.mark.unit


@pytest.fixture
def home(tmp_path):
    db = tmp_path / "installs.db"
    SlackInstallStore(db).upsert(team_id="T1", bot_token="xoxb-SECRET", team_name="Acme")
    GitHubInstallStore(db).upsert(installation_id="42", account_login="acme-org")
    LinearInstallStore(db).upsert(workspace_id="w1", access_token="lin-SECRET", workspace_name="Acme Eng")
    return tmp_path


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["fabri", *argv])
    main()


def test_list_shows_every_surface_without_tokens(home, monkeypatch, capsys):
    run(monkeypatch, "installs", "list", "--home-root", str(home))
    out = capsys.readouterr().out
    assert "slack   T1" in out and "github  42" in out and "linear  w1" in out
    assert "SECRET" not in out
    run(monkeypatch, "installs", "list", "--home-root", str(home), "--surface", "linear", "--json")
    rows = json.loads(capsys.readouterr().out)
    assert [r["workspace_id"] for r in rows] == ["w1"] and "SECRET" not in json.dumps(rows)


def test_remove_deletes_the_install(home, monkeypatch, capsys):
    run(monkeypatch, "installs", "remove", "slack", "T1", "--home-root", str(home))
    assert "removed slack install T1" in capsys.readouterr().out
    assert SlackInstallStore(home / "installs.db").get_token("T1") is None
    with pytest.raises(SystemExit):
        run(monkeypatch, "installs", "remove", "slack", "T1", "--home-root", str(home))


def test_missing_database_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run(monkeypatch, "installs", "list", "--home-root", str(tmp_path / "nope"))
