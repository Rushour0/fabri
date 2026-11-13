"""build_tools under a credential broker (fabri serve) and without one.

The source builder is replaced by a fake here, so these tests pin what
runtime.py does with the broker: create the client from the run env, pass it
to the builder, mark only the tools it routed through the broker, and leave
everything as before when no broker env is set. They also cover the snapshot
check on ``tools.mcp_servers`` ``credential_env``.
"""

import logging

import pytest

import fabri.tools.mcp_client as mcp_client_mod
import fabri.tools.sources as sources_mod
from fabri.runtime import build_tools
from fabri.tools.credential_broker_client import KEY_ENV, SOCKET_ENV, BrokerClient
from fabri.tools.credential_store import credential_env_var
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.result import tool_ok

_KEY = "k" * 64


def _tools_cfg(tmp_path, **extra):
    return {
        "manifest_dir": [],
        "enabled": None,
        "sandbox_root": str(tmp_path),
        "decompose": {"enabled": False, "max_subquestions": 5},
        **extra,
    }


def _http_with_ref(name):
    return {"kind": "http", "name": name, "url": "https://api.example.test/items", "method": "POST",
            "approval": "always_approve",
            "headers": {"Authorization": {"secret_ref": "example:team", "prefix": "Bearer "}}}


def _http_env_only(name):
    return {"kind": "http", "name": name, "url": "https://api.example.test/items", "method": "POST",
            "approval": "always_approve", "headers": {"X-Key": {"env": "EXAMPLE_KEY"}}}


def _python_with_ref(name):
    return {"kind": "python", "name": name, "callable": "os.getcwd",
            "headers": {"Authorization": {"secret_ref": "example:team"}}}


class _Handler:
    """A callable handler that records calls, optionally tagged as a broker
    handler the way the source builder tags one."""

    def __init__(self, gate=None):
        self.calls = []
        if gate is not None:
            self.approval_gate = gate

    def __call__(self, args):
        self.calls.append(args)
        return tool_ok({"status": "ok"})


@pytest.fixture
def fake_builder(monkeypatch):
    """Replace register_tool_sources. Every declaration yields one
    always_approve tool named after it; the handler carries
    ``approval_gate = "broker"`` when the declaration's ``tag_gate`` key says
    so (a builder or a callable claiming the gate), else when a broker was
    passed and the declaration names a secret_ref."""
    seen = {"calls": [], "handlers": {}}

    def fake(registry, decls, **kwargs):
        seen["calls"].append({"decls": list(decls), "kwargs": dict(kwargs)})
        for decl in decls:
            tag = decl.get("tag_gate")
            if tag is None:
                tag = kwargs.get("broker") is not None and bool(sources_mod.secret_refs(decl))
            handler = _Handler("broker" if tag else None)
            seen["handlers"][decl["name"]] = handler
            manifest = ToolManifest(name=decl["name"], description=decl["name"], command=[],
                                    input_schema={"type": "object"}, output_schema={},
                                    approval="always_approve")
            registry.register_callable(manifest, handler)
        return []

    monkeypatch.setattr(sources_mod, "register_tool_sources", fake)
    return seen


@pytest.fixture
def broker_env(monkeypatch, tmp_path):
    monkeypatch.setenv(SOCKET_ENV, str(tmp_path / "brk.sock"))
    monkeypatch.setenv(KEY_ENV, _KEY)


@pytest.fixture
def no_broker_env(monkeypatch):
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)


def test_build_tools_passes_broker_from_env(tmp_path, broker_env, fake_builder):
    reg = build_tools(_tools_cfg(tmp_path, sources=[_http_with_ref("brokered"), _http_env_only("plain")]))

    assert [c["decls"][0]["name"] for c in fake_builder["calls"]] == ["brokered", "plain"]
    for call in fake_builder["calls"]:
        broker = call["kwargs"]["broker"]
        assert isinstance(broker, BrokerClient)
        assert broker.socket_path == str(tmp_path / "brk.sock")
    # One client for the whole build.
    assert fake_builder["calls"][0]["kwargs"]["broker"] is fake_builder["calls"][1]["kwargs"]["broker"]

    # The brokered tool skips the registry approver; the broker asks instead.
    assert reg._broker_gated("brokered") is True
    result = reg.invoke("brokered", {})
    assert result["ok"] is True
    assert fake_builder["handlers"]["brokered"].calls == [{}]

    # A source without a ref stays in-process and keeps the approver.
    assert reg._broker_gated("plain") is False
    result = reg.invoke("plain", {})
    assert result["ok"] is False
    assert "no approver is configured" in result["error"]
    assert fake_builder["handlers"]["plain"].calls == []
    # The manifest class is what lint and Studio show, broker or not.
    assert reg.tools["brokered"].approval == "always_approve"


def test_build_tools_without_env_unchanged(tmp_path, no_broker_env, fake_builder):
    reg = build_tools(_tools_cfg(tmp_path, sources=[_http_with_ref("brokered"), _http_env_only("plain")]))

    for call in fake_builder["calls"]:
        assert "broker" not in call["kwargs"]
    for name in ("brokered", "plain"):
        assert reg._broker_gated(name) is False
        result = reg.invoke(name, {})
        assert result["ok"] is False
        assert "no approver is configured" in result["error"]
        assert fake_builder["handlers"][name].calls == []


def test_build_tools_one_broker_variable_is_not_a_broker(tmp_path, monkeypatch, fake_builder):
    monkeypatch.setenv(SOCKET_ENV, str(tmp_path / "brk.sock"))
    monkeypatch.delenv(KEY_ENV, raising=False)
    reg = build_tools(_tools_cfg(tmp_path, sources=[_http_with_ref("brokered")]))
    assert "broker" not in fake_builder["calls"][0]["kwargs"]
    assert reg._broker_gated("brokered") is False


def test_handler_tag_without_broker_is_ignored(tmp_path, no_broker_env, fake_builder):
    decl = {**_http_with_ref("claims"), "tag_gate": True}
    reg = build_tools(_tools_cfg(tmp_path, sources=[decl]))
    assert fake_builder["handlers"]["claims"].approval_gate == "broker"
    assert reg._broker_gated("claims") is False
    assert reg.invoke("claims", {})["ok"] is False


def test_python_and_ref_less_handlers_cannot_mark_themselves(tmp_path, broker_env, fake_builder):
    py = {**_python_with_ref("py_tool"), "tag_gate": True}
    plain = {**_http_env_only("plain"), "tag_gate": True}
    stdio = {"kind": "mcp", "name": "stdio_src", "command": ["true"], "tag_gate": True,
             "env": {"TOKEN": {"secret_ref": "example:team"}}}
    reg = build_tools(_tools_cfg(tmp_path, sources=[py, plain, stdio]))
    for name in ("py_tool", "plain", "stdio_src"):
        assert fake_builder["handlers"][name].approval_gate == "broker"
        assert reg._broker_gated(name) is False
        result = reg.invoke(name, {})
        assert result["ok"] is False
        assert "no approver is configured" in result["error"]


def test_brokered_ref_without_builder_tag_is_not_marked(tmp_path, broker_env, fake_builder):
    decl = {**_http_with_ref("untagged"), "tag_gate": False}
    reg = build_tools(_tools_cfg(tmp_path, sources=[decl]))
    assert reg._broker_gated("untagged") is False


def test_url_mcp_source_with_ref_is_marked(tmp_path, broker_env, fake_builder):
    decl = {"kind": "mcp", "name": "gh", "url": "https://mcp.example.test/mcp",
            "headers": {"Authorization": {"secret_ref": "github:team", "prefix": "Bearer "}}}
    reg = build_tools(_tools_cfg(tmp_path, sources=[decl]))
    assert reg._broker_gated("gh") is True


def test_skipped_source_tool_never_marks_the_existing_name(tmp_path, broker_env, fake_builder):
    agent = {"name": "brokered", "description": "a sub-agent", "config": "agent.yaml"}
    reg = build_tools(_tools_cfg(tmp_path, agents=[agent], sources=[_http_with_ref("brokered")]))
    assert reg.tools["brokered"].description == "a sub-agent"
    assert reg._broker_gated("brokered") is False


def test_policy_disallowed_source_is_not_marked(tmp_path, broker_env, fake_builder):
    policy = {"allow": ["other"]}
    reg = build_tools(_tools_cfg(tmp_path, sources=[_http_with_ref("brokered")], policy=policy))
    assert "brokered" not in reg.tools
    assert reg._broker_gated("brokered") is False


def test_no_secret_value_in_build_logs(tmp_path, broker_env, fake_builder, caplog):
    with caplog.at_level(logging.DEBUG):
        build_tools(_tools_cfg(tmp_path, sources=[_http_with_ref("brokered")]))
    assert all(_KEY not in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- snapshot credential_env


@pytest.fixture
def fake_mcp(monkeypatch):
    """Record which tools.mcp_servers entries reach build_mcp_tools."""
    started = []

    def fake_build(server_cfg, approval_for=None):
        started.append(server_cfg["name"])
        return object(), []

    monkeypatch.setattr(mcp_client_mod, "build_mcp_tools", fake_build)
    return started


def _snapshot_cfg(tmp_path, credential_env, **extra):
    source = {"kind": "http", "name": "crm", "url": "https://api.example.test/x", "method": "GET",
              "headers": {"Authorization": {"secret_ref": "crm:team"}}}
    server = {"name": "srv", "command": ["true"], "credential_env": credential_env}
    return _tools_cfg(tmp_path, sources=[source], mcp_servers=[server], **extra)


@pytest.fixture
def snapshot_run(monkeypatch, no_broker_env):
    monkeypatch.setenv("FABRI_SESSION_ID", "sess-1")
    monkeypatch.delenv("FABRI_RUN_ACTOR", raising=False)


def test_snapshot_granted_credential_env_starts(tmp_path, snapshot_run, fake_builder, fake_mcp):
    build_tools(_snapshot_cfg(tmp_path, [credential_env_var("crm", "team")]))
    assert fake_mcp == ["srv"]


def test_snapshot_ungranted_credential_env_refused(tmp_path, snapshot_run, fake_builder, fake_mcp, caplog):
    with caplog.at_level(logging.WARNING, logger="fabri.runtime"):
        build_tools(_snapshot_cfg(tmp_path, [credential_env_var("crm", "team"), "FABRI_CRED_GITHUB_ORG"]))
    assert fake_mcp == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("srv" in m and "FABRI_CRED_GITHUB_ORG" in m and "refused" in m for m in messages)


def test_snapshot_policy_grant_limits_credential_env(tmp_path, snapshot_run, fake_builder, fake_mcp):
    policy = {"credential_grants": {"crm": ["crm:other"]}}
    build_tools(_snapshot_cfg(tmp_path, [credential_env_var("crm", "team")], policy=policy))
    assert fake_mcp == []
    policy = {"credential_grants": {"crm": ["crm:team"]}}
    build_tools(_snapshot_cfg(tmp_path, [credential_env_var("crm", "team")], policy=policy))
    assert fake_mcp == ["srv"]


def test_snapshot_actor_ref_follows_run_actor(tmp_path, snapshot_run, monkeypatch, fake_builder, fake_mcp):
    cfg = _snapshot_cfg(tmp_path, [credential_env_var("crm", "u42")])
    cfg["sources"][0]["headers"]["Authorization"]["secret_ref"] = "crm:${actor}"
    build_tools(cfg)
    assert fake_mcp == []
    monkeypatch.setenv("FABRI_RUN_ACTOR", "u42")
    build_tools(cfg)
    assert fake_mcp == ["srv"]


def test_snapshot_never_passes_broker_names(tmp_path, snapshot_run, fake_builder, fake_mcp):
    cfg = _snapshot_cfg(tmp_path, [KEY_ENV])
    cfg["sources"][0]["headers"]["Authorization"]["secret_ref"] = "broker:key"
    assert credential_env_var("broker", "key") == KEY_ENV
    build_tools(cfg)
    assert fake_mcp == []


def test_snapshot_rejects_non_list_credential_env(tmp_path, snapshot_run, fake_builder, fake_mcp):
    build_tools(_snapshot_cfg(tmp_path, credential_env_var("crm", "team")))
    assert fake_mcp == []


def test_snapshot_server_without_credential_env_starts(tmp_path, snapshot_run, fake_builder, fake_mcp):
    cfg = _snapshot_cfg(tmp_path, None)
    build_tools(cfg)
    assert fake_mcp == ["srv"]


def test_fabri_run_credential_env_not_checked(tmp_path, monkeypatch, no_broker_env, fake_builder, fake_mcp):
    monkeypatch.delenv("FABRI_SESSION_ID", raising=False)
    build_tools(_snapshot_cfg(tmp_path, ["FABRI_CRED_GITHUB_ORG"]))
    assert fake_mcp == ["srv"]


def test_broker_run_credential_env_not_checked_here(tmp_path, monkeypatch, broker_env, fake_builder, fake_mcp):
    # Under a broker the run holds no credential values; the stdio client
    # strips the broker names itself.
    monkeypatch.setenv("FABRI_SESSION_ID", "sess-1")
    build_tools(_snapshot_cfg(tmp_path, ["FABRI_CRED_GITHUB_ORG"]))
    assert fake_mcp == ["srv"]
