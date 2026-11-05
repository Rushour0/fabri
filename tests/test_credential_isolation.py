"""Where a run's connector tokens may go.

The run process holds the FABRI_CRED_* values its own sources declare. Its
in-process http and openapi source tools read them there. Every subprocess the
run starts (a manifest tool such as bash, a stdio MCP server, a sub-agent, a
verify command) gets an environment with those values removed, except the exact
names that subprocess's own declaration needs.

The service side picks the install database it resolves from explicitly and
checks a row's scope even when the audit log wraps the store.

No test prints or asserts on a token value: children report variable names, and
values are compared as digests.
"""
from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

from fabri.service.connection_store import ConnectionStore
from fabri.service.launcher import scoped_credential_env
from fabri.tools.credential_store import (
    CredentialNotFoundError,
    EnvCredentialStore,
    ScopedCredentialStore,
    env_without_credentials,
)
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.runner import run_tool
from fabri.tools.sources import build_source_tools

ACME = "acme-isolation-token"
GLOBEX = "globex-isolation-token"
EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "fabri" / "tools" / "examples"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def run_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run process holds two tokens, as it would after the launcher passed them."""
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME", ACME)
    monkeypatch.setenv("FABRI_CRED_SLACK_GLOBEX", GLOBEX)
    monkeypatch.delenv("FABRI_RUN_ACTOR", raising=False)
    monkeypatch.delenv("FABRI_CRED_AUDIT_LOG", raising=False)


def _cred_names(text: str) -> list[str]:
    return sorted({line for line in text.split() if line.startswith("FABRI_CRED_")})


# --- manifest tools ----------------------------------------------------------


def test_bash_tool_sees_no_credentials(tmp_path: Path, run_tokens) -> None:
    manifest = ToolManifest.from_file(EXAMPLES / "bash.json")
    # Names only, so a failure can never print a value.
    out = run_tool(manifest, {"command": "env | cut -d= -f1"},
                   extra_env={"FABRI_SANDBOX_ROOT": str(tmp_path)})
    assert out["ok"], "bash tool failed to run"
    names = out["result"]["stdout"].split()
    assert "PATH" in names
    assert _cred_names(" ".join(names)) == []


def test_manifest_tool_without_extra_env_sees_no_credentials(tmp_path: Path, run_tokens) -> None:
    script = tmp_path / "names.py"
    script.write_text("import json, os, sys; sys.stdin.read(); print(json.dumps({'names': sorted(os.environ)}))\n")
    manifest = ToolManifest(name="names", description="d", command=[sys.executable, str(script)],
                            input_schema={"type": "object"}, output_schema={"type": "object"})
    out = run_tool(manifest, {})
    assert out["ok"]
    assert _cred_names(" ".join(out["result"]["names"])) == []


# --- sub-agents ----------------------------------------------------------------


def _source_config(path: Path, ref: str) -> Path:
    path.write_text(yaml.safe_dump({"tools": {"sources": [{
        "name": "chat", "kind": "http", "url": "https://api.example.test/x", "method": "GET",
        "headers": {"Authorization": {"secret_ref": ref, "prefix": "Bearer "}},
    }]}}))
    return path


def _names_script(path: Path) -> Path:
    path.write_text(textwrap.dedent("""
        import json, os, sys
        sys.stdin.read()
        print(json.dumps({"names": sorted(k for k in os.environ if k.startswith("FABRI_CRED_"))}))
    """))
    return path


def test_static_subagent_keeps_only_its_declared_ref(tmp_path: Path, run_tokens) -> None:
    config = _source_config(tmp_path / "child.yaml", "slack:acme")
    runner_script = _names_script(tmp_path / "agent_runner_tool.py")
    manifest = ToolManifest(name="child", description="d",
                            command=[sys.executable, str(runner_script), str(config)],
                            input_schema={"type": "object"}, output_schema={"type": "object"})
    out = run_tool(manifest, {"task": "t"})
    assert out["ok"]
    assert out["result"]["names"] == ["FABRI_CRED_SLACK_ACME"]


def test_spawned_subagent_keeps_only_its_declared_ref(tmp_path: Path, run_tokens) -> None:
    config = _source_config(tmp_path / "child.yaml", "slack:globex")
    spawn_script = _names_script(tmp_path / "spawn_subagent.py")
    manifest = ToolManifest(name="spawn_subagent", description="d",
                            command=[sys.executable, str(spawn_script)],
                            input_schema={"type": "object"}, output_schema={"type": "object"})
    out = run_tool(manifest, {"config_path": str(config), "task": "t"})
    assert out["ok"]
    assert out["result"]["names"] == ["FABRI_CRED_SLACK_GLOBEX"]

    # A config with no sources gets nothing.
    bare = tmp_path / "bare.yaml"
    bare.write_text("llm:\n  model: m\n")
    out = run_tool(manifest, {"config_path": str(bare), "task": "t"})
    assert out["result"]["names"] == []


# --- stdio MCP servers ----------------------------------------------------------


@pytest.fixture
def env_names_mcp_server(tmp_path: Path) -> Path:
    """A stdio MCP server whose one tool reports which variable names it can see."""
    script = tmp_path / "names_server.py"
    script.write_text(textwrap.dedent("""
        import json, os, sys
        for line in sys.stdin:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            method = msg.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {},
                          "serverInfo": {"name": "names", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "names", "description": "env names",
                                     "inputSchema": {"type": "object"}}]}
            else:
                names = sorted(k for k in os.environ if k.startswith("FABRI_CRED_"))
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"creds": names, "has_token_x": "TOKEN_X" in os.environ})}]}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
            sys.stdout.flush()
    """))
    return script


def _mcp_names(script: Path, env_mode: str) -> dict:
    decl = {"name": "names_srv", "kind": "mcp", "command": [sys.executable, str(script)],
            "env": {"TOKEN_X": {"secret_ref": "slack:acme"}}, "env_mode": env_mode}
    tools = build_source_tools(decl)
    try:
        out = tools[0].handler({})
    finally:
        tools[0].extra["client"].close()
    assert out["ok"], "mcp call failed"
    return json.loads(out["result"]["content"][0]["text"])


def test_stdio_mcp_child_sees_only_its_declared_credential(env_names_mcp_server: Path, run_tokens) -> None:
    seen = _mcp_names(env_names_mcp_server, "inherit")
    assert seen["creds"] == ["FABRI_CRED_SLACK_ACME"]
    assert seen["has_token_x"] is True


def test_allowlist_mcp_child_sees_no_inherited_credential(env_names_mcp_server: Path, run_tokens) -> None:
    seen = _mcp_names(env_names_mcp_server, "allowlist")
    assert seen["creds"] == []
    assert seen["has_token_x"] is True


# --- in-process sources -----------------------------------------------------------


def test_http_source_still_authenticates_from_the_run_env(run_tokens) -> None:
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(_digest(request.headers.get("authorization", "")))
        return httpx.Response(200, json={"ok": True})

    tool = build_source_tools(
        {"kind": "http", "name": "chat", "url": "https://api.example.test/x", "method": "GET",
         "headers": {"Authorization": {"secret_ref": "slack:acme", "prefix": "Bearer "}}},
        transport=httpx.MockTransport(handle), store=EnvCredentialStore())[0]
    out = tool.handler({})
    assert out["ok"], "http source call failed"
    assert sent == [_digest("Bearer " + ACME)], "http source did not send the run's token"


# --- other subprocesses -------------------------------------------------------------


def test_verifier_sees_no_credentials(tmp_path: Path, run_tokens) -> None:
    from fabri.core.agent import _run_verifier

    script = tmp_path / "verify.py"
    script.write_text("import os\nprint(' '.join(sorted(os.environ)))\n")
    verdict = _run_verifier([sys.executable, str(script)], str(tmp_path))
    assert verdict["ok"]
    assert _cred_names(verdict["output"]) == []


def test_env_without_credentials_keeps_exact_names_and_audit_path() -> None:
    env = {"PATH": "/bin", "FABRI_CRED_A_B": "x", "FABRI_CRED_C_D": "y", "FABRI_CRED_AUDIT_LOG": "/tmp/a.jsonl"}
    assert sorted(env_without_credentials(env, keep={"FABRI_CRED_C_D"})) == [
        "FABRI_CRED_AUDIT_LOG", "FABRI_CRED_C_D", "PATH"]


# --- service: which database, which actor ----------------------------------------------


class _UserRows:
    """One user row per user and one org row."""

    rows = {("gmail", "u1"): ("user", "u1-row-token"), ("gmail", "u2"): ("user", "u2-row-token"),
            ("slack", "main"): ("org", "org-row-token")}

    def scope_kind(self, provider, handle):
        row = self.rows.get((provider, handle))
        return row[0] if row else None

    def get(self, provider, handle):
        if (provider, handle) not in self.rows:
            raise CredentialNotFoundError("missing")
        return self.rows[(provider, handle)][1]


def _refs_config(path: Path, refs: list[str]) -> Path:
    sources = [{"name": f"s{i}", "kind": "http", "url": "https://api.example.test/x", "method": "GET",
                "headers": {"Authorization": {"secret_ref": ref}}} for i, ref in enumerate(refs)]
    path.write_text(yaml.safe_dump({"tools": {"sources": sources}}))
    return path


def test_audit_wrapper_keeps_user_rows_to_their_user(tmp_path: Path) -> None:
    config = _refs_config(tmp_path / "run.yaml", ["gmail:u2", "gmail:${actor}", "slack:main"])
    store = ScopedCredentialStore(_UserRows(), audit_path=tmp_path / "audit.jsonl", actor="launcher")

    got = scoped_credential_env(config, store=store, actor="u1")

    assert sorted(got) == ["FABRI_CRED_GMAIL_U1", "FABRI_CRED_SLACK_MAIN"]
    # The audit log never saw a lookup of the other user's row.
    lookups = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert ("gmail", "u2") not in {(e["provider"], e["handle"]) for e in lookups}


def test_store_without_scope_passes_nothing(tmp_path: Path) -> None:
    config = _refs_config(tmp_path / "run.yaml", ["slack:main"])

    class NoScope:
        def get(self, provider, handle):
            return "org-row-token"

    assert scoped_credential_env(config, store=NoScope(), actor="u1") == {}
    # A wrapper around such a store cannot report scope either: still nothing.
    assert scoped_credential_env(config, store=ScopedCredentialStore(NoScope()), actor="u1") == {}


@pytest.fixture
def dumping_command(tmp_path: Path):
    script = tmp_path / "dump.py"
    script.write_text(textwrap.dedent("""
        import hashlib, json, os, pathlib
        home = pathlib.Path(os.environ["FABRI_HOME"])
        sid = os.environ["FABRI_SESSION_ID"]
        trace = home / ".fabri" / "traces" / f"{sid}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("")
        creds = {k: hashlib.sha256(v.encode()).hexdigest()
                 for k, v in os.environ.items() if k.startswith("FABRI_CRED_")}
        print(json.dumps({"session_id": sid, "success": True, "outcome": "success",
                          "final_text": json.dumps({"creds": creds,
                                                    "actor": os.environ.get("FABRI_RUN_ACTOR")})}))
    """))
    return lambda task, cfg, sid, home: [sys.executable, str(script)]


def _service_run(tmp_path: Path, template: Path, command, **submit_kw) -> dict:
    from fabri.service.service import FabriService

    svc = FabriService(template_config=str(template), home_root=tmp_path / "runs", command_builder=command)
    try:
        return json.loads(svc.result(svc.submit("t", **submit_kw), timeout=30)["final_text"])
    finally:
        svc.close()


def test_service_resolves_from_its_own_db_not_the_env_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dumping_command
) -> None:
    for name in ("FABRI_CRED_AUDIT_LOG", "FABRI_CRED_SLACK_MAIN", "FABRI_RUN_ENV_ALLOW"):
        monkeypatch.delenv(name, raising=False)
    other = ConnectionStore(tmp_path / "other" / "installs.db")
    other.upsert(provider="slack", handle="main", access_token="other-db-token")
    monkeypatch.setenv("FABRI_INSTALL_DB", str(tmp_path / "other" / "installs.db"))
    own = ConnectionStore(tmp_path / "runs" / "installs.db")
    own.upsert(provider="slack", handle="main", access_token="own-db-token")
    template = _refs_config(tmp_path / "template.yaml", ["slack:main"])

    seen = _service_run(tmp_path, template, dumping_command)

    assert sorted(seen["creds"]) == ["FABRI_CRED_SLACK_MAIN"]
    assert seen["creds"]["FABRI_CRED_SLACK_MAIN"] == _digest("own-db-token"), "run resolved from the wrong database"


def test_submit_user_id_is_the_run_actor_with_audit_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dumping_command
) -> None:
    for name in ("FABRI_CRED_GMAIL_U1", "FABRI_CRED_GMAIL_U2", "FABRI_RUN_ENV_ALLOW", "FABRI_INSTALL_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_CRED_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    own = ConnectionStore(tmp_path / "runs" / "installs.db")
    own.upsert(provider="gmail", handle="u1", access_token="u1-db-token", scope_kind="user")
    own.upsert(provider="gmail", handle="u2", access_token="u2-db-token", scope_kind="user")
    template = _refs_config(tmp_path / "template.yaml", ["gmail:${actor}", "gmail:u2"])

    seen = _service_run(tmp_path, template, dumping_command, user_id="u1")

    assert seen["actor"] == "u1"
    assert sorted(seen["creds"]) == ["FABRI_CRED_GMAIL_U1"]
    assert seen["creds"]["FABRI_CRED_GMAIL_U1"] == _digest("u1-db-token"), "actor's own row was not passed"

    # Without a user, neither user row reaches the run.
    seen = _service_run(tmp_path, template, dumping_command)
    assert seen["actor"] is None
    assert seen["creds"] == {}


# --- manifest `credentials` field ---------------------------------------------


def _names_manifest(tmp_path: Path, credentials=None) -> ToolManifest:
    script = tmp_path / "names.py"
    script.write_text("import json, os, sys; sys.stdin.read(); print(json.dumps({'names': sorted(os.environ)}))\n")
    data = {"name": "names", "description": "d", "command": [sys.executable, str(script)]}
    if credentials is not None:
        data["credentials"] = credentials
    path = tmp_path / "names.json"
    path.write_text(json.dumps(data))
    return ToolManifest.from_file(path)


def test_declared_pattern_restores_only_matching_names(tmp_path: Path, run_tokens, monkeypatch) -> None:
    monkeypatch.setenv("FABRI_CRED_SLACK_X", "x-value")
    monkeypatch.setenv("FABRI_CRED_CAKEMAIL_MAIN", "c-value")
    out = run_tool(_names_manifest(tmp_path, ["slack:*"]), {})
    assert out["ok"]
    names = _cred_names(" ".join(out["result"]["names"]))
    assert "FABRI_CRED_SLACK_X" in names
    assert "FABRI_CRED_SLACK_ACME" in names
    assert "FABRI_CRED_CAKEMAIL_MAIN" not in names


def test_exact_handle_pattern_restores_one_name(tmp_path: Path, run_tokens) -> None:
    out = run_tool(_names_manifest(tmp_path, ["slack:acme"]), {})
    assert _cred_names(" ".join(out["result"]["names"])) == ["FABRI_CRED_SLACK_ACME"]


def test_no_credentials_field_restores_nothing(tmp_path: Path, run_tokens) -> None:
    out = run_tool(_names_manifest(tmp_path), {})
    assert out["ok"]
    assert _cred_names(" ".join(out["result"]["names"])) == []


@pytest.mark.parametrize("bad", ["slack:*", "slack", [""], ["slack"], ["slack:"], [":x"], ["a:b:c"],
                                 ["sl*ck:x"], ["slack:a*"], ["slack: x"], [1], [None]])
def test_bad_credential_patterns_are_rejected_at_load(tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError):
        _names_manifest(tmp_path, bad)


def test_slack_post_resolves_its_token_through_the_runner(run_tokens, monkeypatch) -> None:
    """The recipe's child process finds its token. Checked by name and digest,
    never by printing the value."""
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", ACME)
    manifest = ToolManifest.from_file(
        Path(__file__).resolve().parents[1] / "src" / "fabri" / "tools" / "recipes" / "slack_post.json")
    assert manifest.credentials == ["slack:*"]
    probe = (
        "import hashlib, json, sys\n"
        "sys.stdin.read()\n"
        "from fabri.tools.secret_refs import resolve_secret\n"
        "print(json.dumps({'digest': hashlib.sha256(resolve_secret('slack:default').encode()).hexdigest()}))\n"
    )
    manifest.command = [sys.executable, "-c", probe]
    out = run_tool(manifest, {})
    assert out["ok"], out.get("error")
    assert out["result"]["digest"] == _digest(ACME)
