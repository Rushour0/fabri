from fabri.service.install_store import LinearInstallStore


def test_upsert_preserves_install_time_and_replaces_token(tmp_path):
    store = LinearInstallStore(tmp_path / "installs.db")

    store.upsert(
        workspace_id="ws-1",
        access_token="lin_oauth_old",
        workspace_name="Old WS",
        scopes="read",
        now=1000.0,
    )
    store.upsert(
        workspace_id="ws-1",
        access_token="lin_oauth_new",
        workspace_name="New WS",
        scopes="read,write",
        now=2000.0,
    )

    row = store.get("ws-1")
    assert row == {
        "workspace_id": "ws-1",
        "workspace_name": "New WS",
        "scopes": "read,write",
        "installed_at": 1000.0,  # preserved on re-install
        "updated_at": 2000.0,
    }
    assert store.get_token("ws-1") == "lin_oauth_new"


def test_get_and_list_are_token_free(tmp_path):
    store = LinearInstallStore(tmp_path / "installs.db")
    store.upsert(workspace_id="ws-2", access_token="secret-token", now=1.0)

    row = store.get("ws-2")
    assert row is not None
    assert "access_token" not in row

    listed = store.list()
    assert len(listed) == 1
    assert "access_token" not in listed[0]
    assert listed[0]["workspace_id"] == "ws-2"


def test_get_token_missing_returns_none(tmp_path):
    store = LinearInstallStore(tmp_path / "installs.db")
    assert store.get_token("nope") is None
    assert store.get("nope") is None


def test_delete(tmp_path):
    store = LinearInstallStore(tmp_path / "installs.db")
    store.upsert(workspace_id="ws-3", access_token="t", now=1.0)
    assert store.delete("ws-3") is True
    assert store.delete("ws-3") is False
    assert store.get("ws-3") is None
