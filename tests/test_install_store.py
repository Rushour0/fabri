from fabri.service.install_store import SlackInstallStore


def test_upsert_preserves_install_time_and_replaces_install_data(tmp_path):
    store = SlackInstallStore(tmp_path / "installs.db")

    store.upsert(
        team_id="T123",
        bot_token="xoxb-old",
        team_name="Old Team",
        scopes="chat:write",
        now=1000.0,
    )
    store.upsert(
        team_id="T123",
        bot_token="xoxb-new",
        team_name="New Team",
        scopes="chat:write,commands",
        now=2000.0,
    )

    row = store.get("T123")
    assert row == {
        "team_id": "T123",
        "team_name": "New Team",
        "scopes": "chat:write,commands",
        "installed_at": 1000.0,
        "updated_at": 2000.0,
    }
    assert store.get_token("T123") == "xoxb-new"


def test_get_and_list_never_include_bot_token(tmp_path):
    store = SlackInstallStore(tmp_path / "installs.db")
    store.upsert(team_id="T123", bot_token="xoxb-secret", now=1000.0)

    row = store.get("T123")
    rows = store.list()

    assert row is not None
    assert "bot_token" not in row
    assert len(rows) == 1
    assert "bot_token" not in rows[0]


def test_unknown_lookup_and_delete_results(tmp_path):
    store = SlackInstallStore(tmp_path / "installs.db")

    assert store.get("unknown") is None
    assert store.get_token("unknown") is None
    assert store.delete("unknown") is False

    store.upsert(team_id="T123", bot_token="xoxb-secret")
    assert store.delete("T123") is True
    assert store.delete("T123") is False
