"""Actor-scoped credentials: ``${actor}`` refs resolve to the run's user, a
user-scoped connection reaches only its own user, and the setups tool set is
cached per actor. No network: every request goes to an httpx.MockTransport."""
from __future__ import annotations

import textwrap
import threading
from pathlib import Path

import httpx
import pytest

from fabri.service.connection_store import ConnectionStore
from fabri.setups import clear_cache, lint_setup, resolve_setup, resolve_tools
from fabri.setups.tool_entries import EntryError, connector_tool_entry
from fabri.tools.credential_store import (
    ACTOR_SENTINEL,
    ActorCredentialStore,
    CredentialNotFoundError,
    CredentialScopeError,
    EnvCredentialStore,
    SqliteInstallCredentialStore,
)
from fabri.tools.secret_refs import default_credential_store, resolve_secret

URL = "https://api.example.test/x"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FABRI_INSTALL_DB", "FABRI_RUN_ACTOR", "FABRI_CRED_AUDIT_LOG",
                 "FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "installs.db"
    store = ConnectionStore(path)
    store.upsert(provider="gmail", handle="a", access_token="tok-for-a", scope_kind="user")
    store.upsert(provider="gmail", handle="b", access_token="tok-for-b", scope_kind="user")
    store.upsert(provider="slack", handle="T1", access_token="tok-org", scope_kind="org")
    return path


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def actor_layer(root: Path) -> list:
    write(root / "tools/tools.yaml", f"""
        sources:
          - kind: http
            name: mail_me
            url: {URL}
            method: GET
            description: Read my mail.
            headers: {{Authorization: {{secret_ref: "gmail:${{actor}}", prefix: "Bearer "}}}}
          - kind: http
            name: plain
            url: https://api.example.test/plain
            method: GET
            description: A source with no credential.
        allow: [mail_me, plain]
    """)
    return [("baseline", root)]


def recording_transport(seen: list):
    def handle(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handle)


# ---- the store wrapper


def test_sentinel_substitutes_actor(db):
    store = ActorCredentialStore(SqliteInstallCredentialStore(db), "a")
    assert store.get("gmail", ACTOR_SENTINEL) == "tok-for-a"
    assert resolve_secret("gmail:${actor}", store) == "tok-for-a"
    assert ActorCredentialStore(SqliteInstallCredentialStore(db), "b").get("gmail", ACTOR_SENTINEL) == "tok-for-b"
    # A user id that is not a valid connect handle is still substituted.
    env = ActorCredentialStore(EnvCredentialStore(), "0f3a9c")
    with pytest.raises(CredentialNotFoundError) as caught:
        env.get("gmail", ACTOR_SENTINEL)
    assert "FABRI_CRED_GMAIL_0F3A9C" in str(caught.value)


def test_sentinel_without_actor_is_scope_error(db):
    store = ActorCredentialStore(SqliteInstallCredentialStore(db), None)
    with pytest.raises(CredentialScopeError) as caught:
        store.get("gmail", ACTOR_SENTINEL)
    assert str(caught.value) == "credential gmail:${actor} needs an actor; this run has none"
    assert "tok-" not in str(caught.value)


def test_other_users_row_is_denied_and_org_row_allowed(db):
    store = ActorCredentialStore(SqliteInstallCredentialStore(db), "a")
    assert store.get("gmail", "a") == "tok-for-a"
    assert store.get("slack", "T1") == "tok-org"
    with pytest.raises(CredentialScopeError) as caught:
        store.get("gmail", "b")
    message = str(caught.value)
    assert message == "credential gmail belongs to another user"
    assert "tok-" not in message and ":b" not in message

    # A run with no actor never receives a user-scoped row, by literal handle either.
    none = ActorCredentialStore(SqliteInstallCredentialStore(db), None)
    assert none.get("slack", "T1") == "tok-org"
    for handle in ("a", "b"):
        with pytest.raises(CredentialScopeError):
            none.get("gmail", handle)


def test_plain_env_store_passes_literal_handles(monkeypatch):
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME", "env-tok")
    assert ActorCredentialStore(EnvCredentialStore(), None).get("slack", "acme") == "env-tok"

    class NoScope:
        def get(self, provider, handle):
            return f"value-{handle}"

    assert ActorCredentialStore(NoScope(), None).get("slack", "acme") == "value-acme"
    assert ActorCredentialStore(NoScope(), "u1").get("slack", ACTOR_SENTINEL) == "value-u1"


def test_unknown_scope_is_refused():
    class Broken:
        def get(self, provider, handle):
            return "should-not-be-returned"

        def scope_kind(self, provider, handle):
            raise RuntimeError("database locked")

    with pytest.raises(CredentialScopeError, match="unknown scope"):
        ActorCredentialStore(Broken(), "u1").get("gmail", "u1")


def test_rewrapping_replaces_the_actor(db):
    inner = ActorCredentialStore(SqliteInstallCredentialStore(db), None)
    outer = ActorCredentialStore(inner, "a")
    assert outer.get("gmail", ACTOR_SENTINEL) == "tok-for-a"
    assert outer.inner is inner.inner


def test_default_store_reads_FABRI_RUN_ACTOR(db, monkeypatch):
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setenv("FABRI_RUN_ACTOR", "b")
    assert resolve_secret("gmail:${actor}") == "tok-for-b"
    with pytest.raises(CredentialScopeError):
        resolve_secret("gmail:a")
    # An explicit actor wins over the environment.
    assert default_credential_store(actor="a").get("gmail", ACTOR_SENTINEL) == "tok-for-a"

    monkeypatch.delenv("FABRI_RUN_ACTOR")
    with pytest.raises(CredentialScopeError, match="needs an actor"):
        resolve_secret("gmail:${actor}")
    with pytest.raises(CredentialScopeError):
        resolve_secret("gmail:b")
    assert resolve_secret("slack:T1") == "tok-org"


def test_default_store_audit_line_has_substituted_handle(db, tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    monkeypatch.setenv("FABRI_CRED_AUDIT_LOG", str(log))
    monkeypatch.setenv("FABRI_RUN_ACTOR", "a")
    assert resolve_secret("gmail:${actor}") == "tok-for-a"
    text = log.read_text()
    assert '"handle": "a"' in text and "tok-" not in text


# ---- the setups tool set


def test_two_actors_same_setup_each_call_carries_own_token_and_cache_never_crosses(db, tmp_path):
    layers = actor_layer(tmp_path / "layer")
    seen: list = []
    transport = recording_transport(seen)
    store = SqliteInstallCredentialStore(db)

    set_a = resolve_tools(layers, transport=transport, store=store, actor="a", catalog=False)
    set_b = resolve_tools(layers, transport=transport, store=store, actor="b", catalog=False)
    assert id(set_a) != id(set_b)
    assert not set_a.errors and not set_b.errors

    assert set_a.tools["mail_me"].handler({})["ok"]
    assert set_b.tools["mail_me"].handler({})["ok"]
    assert seen == ["Bearer tok-for-a", "Bearer tok-for-b"]

    assert resolve_tools(layers, transport=transport, store=store, actor="a", catalog=False) is set_a
    assert resolve_tools(layers, transport=transport, store=store, actor="b", catalog=False) is set_b


def test_resolve_without_actor_reports_and_skips_actor_sources(db, tmp_path):
    layers = actor_layer(tmp_path / "layer")
    seen: list = []
    transport = recording_transport(seen)
    store = SqliteInstallCredentialStore(db)

    shared = resolve_tools(layers, transport=transport, store=store, catalog=False)
    assert set(shared.tools) == {"plain"}
    assert shared.errors == [
        "baseline:mail_me: needs an actor (a secret ref uses ${actor}); resolve this setup with actor=<user id>"]
    for_u = resolve_tools(layers, transport=transport, store=store, actor="a", catalog=False)
    assert for_u is not shared
    assert set(for_u.tools) == {"mail_me", "plain"}
    assert not any("tok-" in e for e in shared.errors)


def test_resolve_with_default_store_uses_given_actor(db, tmp_path, monkeypatch):
    """With no store, an explicit actor still wins over a service env that has none."""
    monkeypatch.setenv("FABRI_INSTALL_DB", str(db))
    layers = actor_layer(tmp_path / "layer")
    seen: list = []
    tools = resolve_tools(layers, transport=recording_transport(seen), actor="b", catalog=False)
    assert tools.tools["mail_me"].handler({})["ok"]
    assert seen == ["Bearer tok-for-b"]


def test_workspace_flag_accepts_sentinel_only():
    from fabri.tools.sources import secret_refs

    entry = connector_tool_entry("slack", workspace=ACTOR_SENTINEL)
    refs = [ref for decl in entry.data["sources"] for ref in secret_refs(decl)]
    assert refs and all(ref.endswith(":${actor}") for ref in refs)
    for bad in ("${user}", "$actor", "{actor}", "-x", "a b"):
        with pytest.raises(EntryError, match="--workspace"):
            connector_tool_entry("slack", workspace=bad)


def test_lint_warns_on_actor_refs(tmp_path):
    resolved = resolve_setup(actor_layer(tmp_path / "layer"))
    hits = [f for f in lint_setup(resolved) if "resolves per user" in f.message]
    assert len(hits) == 1
    hit = hits[0]
    assert hit.level == "warning" and hit.layer == "baseline" and hit.item == "tools/mail_me"
    assert hit.message == ("tools/mail_me: credential gmail:${actor} resolves per user; "
                           "runs without an actor cannot use it")


# ---- the preview route


AUTH = """auth:
  enabled: true
  secure_cookie: false
  signup: true
  admins: [boss@x.test]
  allowed_emails: [boss@x.test, dev@x.test]
"""


@pytest.fixture
def preview_server(tmp_path, monkeypatch, db):
    from fabri.service.http_server import serve_http
    from fabri.service.service import FabriService
    from fabri.service.setups_routes import SetupsApi

    for name in ("FABRI_AUTH_ALLOWED_EMAILS", "FABRI_AUTH_ADMINS", "FABRI_AUTH_SIGNUP",
                 "FABRI_AUTH_DB_PATH", "FABRI_AUTH_ENABLED", "FABRI_AUTH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_AUTH_SECRET", "secret")
    monkeypatch.setenv("FABRI_SERVICE_TOKEN", "svc-token-value")
    # Word-overlap ranking, so the preview never loads an embedding model.
    import fabri.memory.embeddings as emb
    import fabri.orchestrator.retrieval as retrieval

    monkeypatch.setattr(emb, "embeddings_available", lambda: False)
    monkeypatch.setattr(retrieval, "embeddings_available", lambda: False)
    root = tmp_path / "setups"
    actor_layer(root / "baseline")
    write(root / "baseline/agents/mailer/agent.yaml", """
        title: Mailer
        tools: [mail_me, plain]
    """)
    write(root / "baseline/agents/mailer/prompt.md", "Read the user's mail.\n")
    seen: list = []
    api = SetupsApi(root, layers=[{"kind": "baseline", "path": "baseline"}], policy="permissive",
                    transport=recording_transport(seen), store=SqliteInstallCredentialStore(db))
    auth_path = tmp_path / "fabri-service.yaml"
    auth_path.write_text(AUTH)
    service = FabriService(home_root=tmp_path / "home", auth_config=auth_path)
    server = serve_http(service, port=0, setups_api=api)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import fabri.setups.agent as agent_mod

    captured: list = []
    real = agent_mod.build_agent_config

    def spy(resolved, agent_type, *args, **kwargs):
        captured.append(kwargs.get("tool_set"))
        return real(resolved, agent_type, *args, **kwargs)

    monkeypatch.setattr(agent_mod, "build_agent_config", spy)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, service, captured, seen
    server.shutdown()
    server.server_close()
    service.close()


def _session_for(service, email: str) -> tuple[str, dict]:
    from fabri.service.auth import sign_session

    users = service.user_store
    user_id = users.verify_user(email, "password-1") or users.create_user(email, "password-1")
    return user_id, {"Cookie": f"fabri_session={sign_session(user_id, 'secret', 60)}"}


def test_preview_passes_user_id_as_actor(preview_server, db):
    base, service, captured, seen = preview_server
    user_id, headers = _session_for(service, "dev@x.test")
    ConnectionStore(db).upsert(provider="gmail", handle=user_id, access_token="tok-for-dev", scope_kind="user")

    params = {"type": "mailer", "task": "read my mail"}
    r = httpx.get(f"{base}/setups/preview", params=params, headers=headers)
    assert r.status_code == 200, r.text
    assert "mail_me" in {t["name"] for t in r.json()["tools"]}
    assert "tok-for-dev" not in r.text
    tool_set = captured[-1]
    assert tool_set.tools["mail_me"].handler({})["ok"]
    assert seen == ["Bearer tok-for-dev"]

    # A token caller has no user id, so the preview has no actor and the
    # per-user source is reported and left out.
    r = httpx.get(f"{base}/setups/preview", params=params,
                  headers={"Authorization": "Bearer svc-token-value"})
    assert r.status_code == 200, r.text
    assert "mail_me" not in {t["name"] for t in r.json()["tools"]}
    assert "needs an actor" in " ".join(captured[-1].errors)
