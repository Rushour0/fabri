"""The child environment allowlist: service secrets must not reach a run.

A run executes model-chosen tool calls, and several bundled agencies enable
`bash`. Anything in the run's environment is therefore readable by whatever the
model decides to execute, so the launcher builds that environment from an
allowlist instead of inheriting the service's.

These tests assert the property directly (a real subprocess dumping its own
env), not just the shape of the dict.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fabri.service.launcher import build_child_env, launch_run
from fabri.service.service import _api_key_envs, _bound_api_key_envs

# What a compromised run would go looking for. Every one of these is real: the
# service sets them, and each is either a credential or a path to one.
SECRETS = {
    "FABRI_AUTH_SECRET": "sign-my-own-session",
    "FABRI_ADMIN_TOKEN": "admin",
    "FABRI_INSTALL_DB": "/app/.fabri/serve/installs.db",
    "FABRI_CRED_GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----",
    "FABRI_CRED_SLACK_ACME_ENG": "xoxb-tenant-token",
    "FABRI_SLACK_BOT_TOKEN": "xoxb-service-token",
    "SLACK_SIGNING_SECRET": "sig",
    "SLACK_CLIENT_SECRET": "client",
    "GITHUB_APP_SLUG": "fabri-app",
    "GITHUB_APP_WEBHOOK_SECRET": "hook",
    "LINEAR_CLIENT_SECRET": "linear",
    "FABRI_CONFIG": "/etc/fabri/other.yaml",
}


def test_secrets_are_dropped_and_runtime_survives():
    base = {
        **SECRETS,
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "LC_ALL": "C.UTF-8",
        "OPENAI_API_KEY": "sk-provider",
        "QDRANT_URL": "http://localhost:6333",
        "FABRI_SANDBOX_ROOT": "/work",
        "SOME_UNRELATED_TOKEN": "nope",
    }

    child = build_child_env(base)

    for name in SECRETS:
        assert name not in child, f"{name} leaked into the run environment"
    assert "SOME_UNRELATED_TOKEN" not in child, "allowlist must not be a denylist"
    # The run still has to work.
    assert child["PATH"] == "/usr/bin"
    assert child["HOME"] == "/home/agent"
    assert child["LC_ALL"] == "C.UTF-8"
    assert child["OPENAI_API_KEY"] == "sk-provider"
    assert child["QDRANT_URL"] == "http://localhost:6333"
    assert child["FABRI_SANDBOX_ROOT"] == "/work"


def test_caller_allowlist_readmits_named_and_prefixed():
    base = {"MY_PROVIDER_KEY": "k", "FABRI_CRED_SMTP_DEFAULT": "s", "OTHER": "x"}

    named = build_child_env(base, allow=["MY_PROVIDER_KEY"])
    assert named["MY_PROVIDER_KEY"] == "k"
    assert "FABRI_CRED_SMTP_DEFAULT" not in named
    assert "OTHER" not in named

    prefixed = build_child_env(base, allow=["FABRI_CRED_*"])
    assert prefixed["FABRI_CRED_SMTP_DEFAULT"] == "s"
    assert "MY_PROVIDER_KEY" not in prefixed


def test_operator_env_var_readmits_without_a_code_change():
    """A self-hosted crew that genuinely needs a connector credential says so in
    the deployment, not in a patch."""
    base = {
        "FABRI_RUN_ENV_ALLOW": "FABRI_CRED_*, MY_TOKEN",
        "FABRI_CRED_SLACK_ACME_ENG": "xoxb",
        "MY_TOKEN": "t",
        "FABRI_AUTH_SECRET": "still-secret",
    }

    child = build_child_env(base)

    assert child["FABRI_CRED_SLACK_ACME_ENG"] == "xoxb"
    assert child["MY_TOKEN"] == "t"
    # Re-admitting one thing must not re-admit everything.
    assert "FABRI_AUTH_SECRET" not in child


def test_scrub_can_be_turned_off_for_trusted_callers():
    base = {"FABRI_AUTH_SECRET": "s", "PATH": "/usr/bin"}
    assert build_child_env(base, scrub=False) == base


def test_api_key_envs_walks_nested_role_configs():
    config = {
        "llm": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        "agents": [
            {"llm": {"api_key_env": "ANTHROPIC_API_KEY"}},
            {"llm": {"api_key_env": "OPENAI_API_KEY"}},  # deduped
        ],
        "manager": {"llm": {"api_key_env": "MY_CUSTOM_KEY"}},
    }

    assert _api_key_envs(config) == (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MY_CUSTOM_KEY",
    )
    assert _api_key_envs(None) == ()


def test_bound_api_key_envs_reads_the_written_run_yaml(tmp_path: Path):
    """The template is a path and overrides merge onto its raw YAML, so the
    bound run.yaml is what the launcher must be told about."""
    run_yaml = tmp_path / "run.yaml"
    run_yaml.write_text("llm:\n  api_key_env: MY_CUSTOM_KEY\n")

    assert _bound_api_key_envs(run_yaml) == ("MY_CUSTOM_KEY",)
    assert _bound_api_key_envs(tmp_path / "missing.yaml") == ()


@pytest.fixture
def env_dumping_agent(tmp_path: Path) -> Path:
    """A stand-in for a `bash`-enabled run: it prints the env it can read."""
    script = tmp_path / "dump_env.py"
    script.write_text(textwrap.dedent("""
        import json, os
        print(json.dumps({"env": sorted(os.environ)}))
    """))
    return script


def test_launched_child_cannot_read_service_secrets(
    tmp_path: Path, env_dumping_agent: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: the property that actually matters, observed from the child."""
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider")

    handle = launch_run(
        "t",
        config_path=tmp_path / "unused.yaml",
        fabri_home=tmp_path / "home",
        session_id="sess-scrub",
        command=[sys.executable, str(env_dumping_agent)],
    )
    seen = set(handle.result(timeout=30)["env"])

    assert not (seen & set(SECRETS)), f"run could read: {sorted(seen & set(SECRETS))}"
    # ...while keeping what a run legitimately needs.
    assert "OPENAI_API_KEY" in seen
    assert {"FABRI_HOME", "FABRI_SESSION_ID"} <= seen


def test_bash_tool_in_a_run_sees_the_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The threat model is one step further out: a shell *inside* the run. A
    child of the child inherits the same scrubbed env, so `env | grep SECRET`
    finds nothing."""
    monkeypatch.setenv("FABRI_AUTH_SECRET", "sign-my-own-session")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider")

    script = tmp_path / "shell_out.py"
    script.write_text(textwrap.dedent("""
        import json, subprocess, sys
        out = subprocess.run(
            ["env"], capture_output=True, text=True, check=False
        ).stdout
        print(json.dumps({"grandchild_env": out}))
    """))

    handle = launch_run(
        "t",
        config_path=tmp_path / "unused.yaml",
        fabri_home=tmp_path / "home",
        session_id="sess-shell",
        command=[sys.executable, str(script)],
    )
    dumped = handle.result(timeout=30)["grandchild_env"]

    assert "sign-my-own-session" not in dumped
    assert "FABRI_AUTH_SECRET" not in dumped
    assert "sk-provider" in dumped


def test_real_service_run_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The path that matters in production: FabriService.submit -> launch_run."""
    from fabri.service.service import FabriService

    monkeypatch.setenv("FABRI_INSTALL_DB", "/app/.fabri/serve/installs.db")
    monkeypatch.setenv("MY_CUSTOM_KEY", "sk-custom")

    script = tmp_path / "dump.py"
    script.write_text(textwrap.dedent("""
        import json, os, pathlib
        home = pathlib.Path(os.environ["FABRI_HOME"])
        sid = os.environ["FABRI_SESSION_ID"]
        trace = home / ".fabri" / "traces" / f"{sid}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("")
        print(json.dumps({
            "session_id": sid,
            "success": True,
            "outcome": "success",
            "final_text": json.dumps(sorted(os.environ)),
        }))
    """))

    template = tmp_path / "template.yaml"
    template.write_text("llm:\n  api_key_env: MY_CUSTOM_KEY\n")

    svc = FabriService(
        template_config=str(template),
        home_root=tmp_path / "runs",
        command_builder=lambda task, cfg, sid, home: [sys.executable, str(script)],
    )
    session_id = svc.submit("t")
    seen = set(json.loads(svc.result(session_id, timeout=30)["final_text"]))

    assert "FABRI_INSTALL_DB" not in seen
    # The config named a key the launcher has never heard of; it survived.
    assert "MY_CUSTOM_KEY" in seen


# --- scoped credentials: a run gets the refs its own sources declare ---------

# Stand-in token values. Tests compare digests so no value is ever printed,
# even in a failure message.
ACME = "acme-token-value"
GLOBEX = "globex-token-value"


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def cred_dumping_agent(tmp_path: Path) -> Path:
    """Prints every env name the run sees, and a digest of each FABRI_CRED_* value."""
    script = tmp_path / "dump_creds.py"
    script.write_text(textwrap.dedent("""
        import hashlib, json, os
        print(json.dumps({
            "names": sorted(os.environ),
            "creds": {k: hashlib.sha256(v.encode()).hexdigest()
                      for k, v in os.environ.items() if k.startswith("FABRI_CRED_")},
        }))
    """))
    return script


def _run_yaml(path: Path, sources: list, policy=None) -> Path:
    import yaml

    tools: dict = {"sources": sources}
    if policy is not None:
        tools["policy"] = policy
    path.write_text(yaml.safe_dump({"tools": tools}))
    return path


def _http_source(name: str, ref: str) -> dict:
    return {
        "name": name,
        "kind": "http",
        "url": "https://api.example.test/x",
        "method": "GET",
        "headers": {"Authorization": {"secret_ref": ref, "prefix": "Bearer "}},
    }


@pytest.fixture
def two_tenants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The service process holds both tenants' tokens and an install db path
    (that does not exist, so lookups fall to the env store)."""
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME", ACME)
    monkeypatch.setenv("FABRI_CRED_SLACK_GLOBEX", GLOBEX)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(tmp_path / "absent-installs.db"))
    monkeypatch.delenv("FABRI_RUN_ENV_ALLOW", raising=False)
    monkeypatch.delenv("FABRI_CRED_AUDIT_LOG", raising=False)
    monkeypatch.delenv("FABRI_CRED_SNAPSHOT", raising=False)


def _launch(tmp_path: Path, config: Path, script: Path, sid: str, **kw) -> dict:
    handle = launch_run(
        "t",
        config_path=config,
        fabri_home=tmp_path / sid,
        session_id=sid,
        command=[sys.executable, str(script)],
        **kw,
    )
    return handle.result(timeout=30)


def test_no_cred_values_in_child_without_snapshot(
    tmp_path: Path, cred_dumping_agent: Path, two_tenants
):
    """By default the launcher resolves nothing: a run that declares a stored
    credential gets no FABRI_CRED_* value, even though the service holds it."""
    config = _run_yaml(tmp_path / "run.yaml", [_http_source("chat", "slack:acme")])

    seen = _launch(tmp_path, config, cred_dumping_agent, "sess-default")

    assert seen["creds"] == {}
    assert "FABRI_INSTALL_DB" not in seen["names"]


def test_approval_timeout_reaches_the_run():
    """The run's broker client waits as long as the service will wait for a
    human, so the timeout has to cross the allowlist."""
    child = build_child_env({"FABRI_APPROVAL_TIMEOUT_S": "900"})
    assert child["FABRI_APPROVAL_TIMEOUT_S"] == "900"


# The snapshot path below is the FABRI_CRED_SNAPSHOT=1 opt-out: the service
# passes credential_snapshot=True and the run gets its granted values.


def test_run_gets_only_its_own_refs_never_another_tenants(
    tmp_path: Path, cred_dumping_agent: Path, two_tenants
):
    acme_yaml = _run_yaml(tmp_path / "acme.yaml", [_http_source("chat", "slack:acme")])
    globex_yaml = _run_yaml(tmp_path / "globex.yaml", [_http_source("chat", "slack:globex")])

    acme = _launch(tmp_path, acme_yaml, cred_dumping_agent, "sess-acme", credential_snapshot=True)
    globex = _launch(tmp_path, globex_yaml, cred_dumping_agent, "sess-globex", credential_snapshot=True)

    assert sorted(acme["creds"]) == ["FABRI_CRED_SLACK_ACME"]
    acme_ok = acme["creds"]["FABRI_CRED_SLACK_ACME"] == _digest(ACME)
    assert acme_ok, "acme run did not get the acme value"
    assert sorted(globex["creds"]) == ["FABRI_CRED_SLACK_GLOBEX"]
    globex_ok = globex["creds"]["FABRI_CRED_SLACK_GLOBEX"] == _digest(GLOBEX)
    assert globex_ok, "globex run did not get the globex value"
    for seen in (acme, globex):
        assert "FABRI_INSTALL_DB" not in seen["names"]


def test_credential_grants_filter_the_declared_refs(
    tmp_path: Path, cred_dumping_agent: Path, two_tenants
):
    """A source that declares a ref its policy does not grant gets nothing for it."""
    config = _run_yaml(
        tmp_path / "run.yaml",
        [_http_source("acme_chat", "slack:acme"), _http_source("other_chat", "slack:globex")],
        policy={"credential_grants": {"acme_*": ["slack:acme"], "other_*": ["linear:*"]}},
    )

    seen = _launch(tmp_path, config, cred_dumping_agent, "sess-grants", credential_snapshot=True)

    assert sorted(seen["creds"]) == ["FABRI_CRED_SLACK_ACME"]
    assert "FABRI_INSTALL_DB" not in seen["names"]


def test_run_with_no_secret_refs_gets_no_credentials(
    tmp_path: Path, cred_dumping_agent: Path, two_tenants
):
    plain = {"name": "pub", "kind": "http", "url": "https://api.example.test/x", "method": "GET"}
    with_sources = _run_yaml(tmp_path / "plain.yaml", [plain])
    no_tools = tmp_path / "bare.yaml"
    no_tools.write_text("llm:\n  model: m\n")

    for sid, config in (("sess-plain", with_sources), ("sess-bare", no_tools),
                        ("sess-missing", tmp_path / "missing.yaml")):
        seen = _launch(tmp_path, config, cred_dumping_agent, sid, credential_snapshot=True)
        assert seen["creds"] == {}, sid
        assert "FABRI_INSTALL_DB" not in seen["names"], sid


def test_install_db_never_reaches_a_run_even_when_named(
    tmp_path: Path, cred_dumping_agent: Path, two_tenants, monkeypatch: pytest.MonkeyPatch
):
    """No allowlist, operator setting or caller env puts the install db in a run."""
    monkeypatch.setenv("FABRI_RUN_ENV_ALLOW", "FABRI_INSTALL_DB,FABRI_INSTALL_*")
    config = _run_yaml(tmp_path / "run.yaml", [_http_source("chat", "slack:acme")])

    seen = _launch(
        tmp_path, config, cred_dumping_agent, "sess-db", credential_snapshot=True,
        allow_env=("FABRI_INSTALL_DB",),
        env={"FABRI_INSTALL_DB": "/elsewhere/installs.db"},
    )

    assert "FABRI_INSTALL_DB" not in seen["names"]
    assert "FABRI_INSTALL_DB" not in build_child_env(
        {"FABRI_INSTALL_DB": "/x", "FABRI_RUN_ENV_ALLOW": "FABRI_INSTALL_DB"}, allow=["FABRI_*"])


class _FakeStore:
    """A store with one org row and one user row per provider; records lookups."""

    def __init__(self) -> None:
        self.rows = {("gmail", "u1"): ("user", "u1-token"), ("gmail", "u2"): ("user", "u2-token"),
                     ("cakemail", "main"): ("org", "org-token")}
        self.lookups: list[tuple[str, str]] = []

    def scope_kind(self, provider, handle):
        row = self.rows.get((provider, handle))
        return row[0] if row else None

    def get(self, provider, handle):
        from fabri.tools.credential_store import CredentialNotFoundError

        self.lookups.append((provider, handle))
        if (provider, handle) not in self.rows:
            raise CredentialNotFoundError("missing")
        return self.rows[(provider, handle)][1]


def test_actor_refs_and_user_rows_go_only_to_that_actor(tmp_path: Path):
    from fabri.service.launcher import scoped_credential_env

    config = _run_yaml(tmp_path / "run.yaml", [
        _http_source("mail", "gmail:${actor}"),
        _http_source("other_mail", "gmail:u2"),
        _http_source("news", "cakemail:main"),
        _http_source("gone", "cakemail:nobody"),
    ])

    store = _FakeStore()
    got = scoped_credential_env(config, store=store, actor="u1")
    assert sorted(got) == ["FABRI_CRED_CAKEMAIL_MAIN", "FABRI_CRED_GMAIL_U1"]
    # u2's row was never even read for u1's run.
    assert ("gmail", "u2") not in store.lookups

    no_actor = scoped_credential_env(config, store=_FakeStore(), actor=None)
    assert sorted(no_actor) == ["FABRI_CRED_CAKEMAIL_MAIN"]


def test_scoped_credentials_skip_malformed_refs_and_bad_policy(tmp_path: Path):
    from fabri.service.launcher import granted_secret_refs

    sources = [_http_source("a", "no-colon"), _http_source("b", "x:y:z"), _http_source("c", ":h"),
               _http_source("d", "slack:acme")]
    assert granted_secret_refs({"tools": {"sources": sources}}) == [("slack", "acme")]
    # A policy that does not parse passes nothing rather than everything.
    assert granted_secret_refs({"tools": {"sources": sources, "policy": {"not_a_key": 1}}}) == []
    assert granted_secret_refs({"tools": {"sources": sources, "policy": str(tmp_path / "nope.yaml")}}) == []
    assert granted_secret_refs({"tools": "x"}) == []


_SERVICE_DUMP = """
import hashlib, json, os, pathlib
home = pathlib.Path(os.environ["FABRI_HOME"])
sid = os.environ["FABRI_SESSION_ID"]
trace = home / ".fabri" / "traces" / f"{sid}.jsonl"
trace.parent.mkdir(parents=True, exist_ok=True)
trace.write_text("")
creds = {k: hashlib.sha256(v.encode()).hexdigest()
         for k, v in os.environ.items() if k.startswith("FABRI_CRED_")}
sock = os.environ.get("FABRI_CRED_BROKER_SOCKET")
print(json.dumps({"session_id": sid, "success": True, "outcome": "success",
                  "final_text": json.dumps({"names": sorted(os.environ), "creds": creds,
                                            "socket_seen": bool(sock and os.path.exists(sock)),
                                            "socket": sock})}))
"""


def _service_run(tmp_path: Path, template: Path) -> tuple[dict, object, str]:
    from fabri.service.service import FabriService

    script = tmp_path / "dump.py"
    script.write_text(_SERVICE_DUMP)
    svc = FabriService(
        template_config=str(template),
        home_root=tmp_path / "runs",
        command_builder=lambda task, cfg, sid, home: [sys.executable, str(script)],
    )
    try:
        sid = svc.submit("t")
        seen = json.loads(svc.result(sid, timeout=30)["final_text"])
        return seen, svc.credential_broker.bindings(), sid
    finally:
        svc.close()


def test_real_service_run_gets_broker_env_and_no_values(tmp_path: Path, two_tenants, monkeypatch):
    # Same-user runs need the local-development opt-in since stored
    # credentials are refused otherwise.
    monkeypatch.setenv("FABRI_ALLOW_SHARED_UID", "1")
    """FabriService.submit -> launch_run: the run gets the broker's socket and
    key, never the token the service holds for its declared ref."""
    template = _run_yaml(tmp_path / "template.yaml", [_http_source("chat", "slack:acme")])

    seen, live_bindings, sid = _service_run(tmp_path, template)

    assert sorted(seen["creds"]) == ["FABRI_CRED_BROKER_KEY", "FABRI_CRED_BROKER_SOCKET"]
    assert _digest(ACME) not in seen["creds"].values()
    assert _digest(GLOBEX) not in seen["creds"].values()
    assert seen["socket_seen"] is True
    assert "FABRI_INSTALL_DB" not in seen["names"]
    # result() ended the run, so its binding and socket are gone.
    assert sid not in live_bindings
    assert not Path(seen["socket"]).exists()


def test_snapshot_opt_out_restores_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, two_tenants
):
    """FABRI_CRED_SNAPSHOT=1 on the service: the bound run.yaml decides which
    values the run gets, and no broker is bound."""
    monkeypatch.setenv("FABRI_CRED_SNAPSHOT", "1")
    template = _run_yaml(tmp_path / "template.yaml", [_http_source("chat", "slack:acme")])

    seen, _bindings, _sid = _service_run(tmp_path, template)

    assert sorted(seen["creds"]) == ["FABRI_CRED_SLACK_ACME"]
    acme_ok = seen["creds"]["FABRI_CRED_SLACK_ACME"] == _digest(ACME)
    assert acme_ok, "the run did not get its declared value"
    assert seen["socket"] is None
    assert "FABRI_INSTALL_DB" not in seen["names"]
