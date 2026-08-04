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
