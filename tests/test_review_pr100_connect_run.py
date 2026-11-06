"""PR #100 review: a connection stored with `fabri connect` (into
$FABRI_HOME/serve/installs.db) resolves in a direct `fabri run`, while a run
`fabri serve` launched never opens the install database."""
from __future__ import annotations

import argparse
import contextvars
import os
import sqlite3
import threading

import pytest

from fabri import cli
from fabri.tools import secret_refs
from fabri.tools.credential_broker_client import BROKER_ENV_NAMES
from fabri.tools.credential_store import CredentialNotFoundError
from fabri.tools.secret_refs import resolve_secret

TOKEN = "lin_api_stored_token_5d1e9a"


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("FABRI_INSTALL_DB", "FABRI_SESSION_ID", "FABRI_RUN_ACTOR", "FABRI_CRED_AUDIT_LOG",
                 "FABRI_CRED_LINEAR_ACME", *BROKER_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FABRI_HOME", str(tmp_path))
    token = secret_refs._cli_install_db.set(None)
    yield tmp_path
    secret_refs._cli_install_db.reset(token)


def _connect(home) -> None:
    """Write the row the way `fabri connect` does: through its own store
    opener, with no --home-root and no FABRI_INSTALL_DB."""
    store = cli._connection_store(argparse.Namespace(home_root=None), create=True)
    store.upsert(provider="linear", handle="acme", access_token=TOKEN)
    assert (home / "serve" / "installs.db").is_file()


def _run_args(**kw) -> argparse.Namespace:
    return argparse.Namespace(config="unused.yaml", task="t", dry_run=True, **kw)


def _dry_run_resolving(monkeypatch, seen: dict):
    """cmd_run with the config load stubbed; the dry-run step resolves the
    ref the way a tool source does (default store, no store passed)."""
    monkeypatch.setattr(cli, "load_config", lambda path: {})

    def fake_dry_run(config, task):
        try:
            seen["value"] = resolve_secret("linear:acme")
        except CredentialNotFoundError as exc:
            seen["error"] = str(exc)

    monkeypatch.setattr(cli, "_print_dry_run", fake_dry_run)


def test_connect_then_run_resolves_the_stored_connection(home, monkeypatch):
    _connect(home)
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert seen == {"value": TOKEN}
    # The path is never exported, so tool subprocesses cannot inherit it.
    assert "FABRI_INSTALL_DB" not in os.environ


def test_without_the_cli_hook_the_ref_is_missing(home):
    # The bug the fix closes: the default store alone does not see the db.
    _connect(home)
    with pytest.raises(CredentialNotFoundError):
        resolve_secret("linear:acme")


def test_missing_db_falls_back_to_env(home, monkeypatch):
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    monkeypatch.setenv("FABRI_CRED_LINEAR_ACME", "from-env-value")
    cli.cmd_run(_run_args())
    assert seen == {"value": "from-env-value"}
    assert secret_refs._cli_install_db.get() is None


def _forbid_db(monkeypatch, home):
    db = str(home / "serve" / "installs.db")
    real = sqlite3.connect

    def guarded(path, *a, **kw):
        if str(path) == db:
            raise AssertionError("service child opened the install database")
        return real(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", guarded)

    def no_store(*a, **kw):
        raise AssertionError("service child built an install-db store")

    monkeypatch.setattr(secret_refs, "SqliteInstallCredentialStore", no_store)


@pytest.mark.parametrize("env_name", [*sorted(BROKER_ENV_NAMES), "FABRI_SESSION_ID"])
def test_service_child_never_opens_the_db(home, monkeypatch, env_name):
    _connect(home)
    monkeypatch.setenv(env_name, "set-by-the-launcher")
    _forbid_db(monkeypatch, home)
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert "value" not in seen
    assert "error" in seen
    assert secret_refs._cli_install_db.get() is None


def test_store_refuses_db_if_broker_env_appears_later(home, monkeypatch):
    # Even with the process-level path set, a broker env at lookup time wins.
    _connect(home)
    with secret_refs.cli_install_db(home / "serve" / "installs.db"):
        assert secret_refs._cli_install_db.get() is not None
        monkeypatch.setenv(sorted(BROKER_ENV_NAMES)[0], "/tmp/broker.sock")
        _forbid_db(monkeypatch, home)
        with pytest.raises(CredentialNotFoundError):
            resolve_secret("linear:acme")


def test_explicit_install_db_env_still_wins(home, tmp_path, monkeypatch):
    _connect(home)
    other = tmp_path / "other.db"
    monkeypatch.setenv("FABRI_INSTALL_DB", str(other))
    store = cli._connection_store(argparse.Namespace(home_root=None), create=True)
    store.upsert(provider="linear", handle="acme", access_token="other-db-token-77")
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert seen == {"value": "other-db-token-77"}


@pytest.mark.parametrize("env_name", [*sorted(BROKER_ENV_NAMES), "FABRI_SESSION_ID"])
def test_service_child_ignores_install_db_env_too(home, monkeypatch, env_name):
    # FABRI_INSTALL_DB in a service child's env (an operator export, a
    # re-admitted var) still does not open the database.
    _connect(home)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(home / "serve" / "installs.db"))
    monkeypatch.setenv(env_name, "set-by-the-launcher")
    _forbid_db(monkeypatch, home)
    with pytest.raises(CredentialNotFoundError):
        resolve_secret("linear:acme")
    seen: dict = {}
    _dry_run_resolving(monkeypatch, seen)
    cli.cmd_run(_run_args())
    assert "value" not in seen and "error" in seen


def test_service_child_snapshot_values_still_resolve(home, monkeypatch):
    # FABRI_CRED_SNAPSHOT mode: the launcher hands the child FABRI_CRED_*
    # values, which the env store reads even with FABRI_INSTALL_DB present.
    _connect(home)
    monkeypatch.setenv("FABRI_INSTALL_DB", str(home / "serve" / "installs.db"))
    monkeypatch.setenv("FABRI_SESSION_ID", "s-1")
    monkeypatch.setenv("FABRI_CRED_LINEAR_ACME", "snapshot-value-1234")
    _forbid_db(monkeypatch, home)
    assert resolve_secret("linear:acme") == "snapshot-value-1234"


def _store_token(db_dir, token: str) -> None:
    store = cli._connection_store(argparse.Namespace(home_root=str(db_dir)), create=True)
    store.upsert(provider="linear", handle="acme", access_token=token)


def test_concurrent_runs_each_resolve_their_own_db(home, tmp_path, monkeypatch):
    # Two direct runs in one process, in separate threads, overlap in time:
    # each resolves from its own --home-root database, never the other's.
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    _store_token(a_dir, "token-from-db-a-111")
    _store_token(b_dir, "token-from-db-b-222")
    monkeypatch.setattr(cli, "load_config", lambda path: {})
    both_inside = threading.Barrier(2, timeout=10)
    seen: dict[str, object] = {}

    def fake_dry_run(config, task):
        # Both runs have selected their db before either resolves.
        both_inside.wait()
        seen[task] = resolve_secret("linear:acme")
        # The agent's parallel tool pool runs each call in a copied context;
        # a lookup there still sees this run's db.
        ctx = contextvars.copy_context()
        result: dict = {}
        worker = threading.Thread(target=ctx.run, args=(lambda: result.update(v=resolve_secret("linear:acme")),))
        worker.start()
        worker.join()
        seen[task + ":worker"] = result.get("v")
        both_inside.wait()

    monkeypatch.setattr(cli, "_print_dry_run", fake_dry_run)

    def run(task, root):
        try:
            cli.cmd_run(argparse.Namespace(config="unused.yaml", task=task, dry_run=True, home_root=str(root)))
        except Exception as exc:  # noqa: BLE001
            seen[task] = exc

    threads = [threading.Thread(target=run, args=("a", a_dir)), threading.Thread(target=run, args=("b", b_dir))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert seen == {
        "a": "token-from-db-a-111", "a:worker": "token-from-db-a-111",
        "b": "token-from-db-b-222", "b:worker": "token-from-db-b-222",
    }
    # Nothing leaked into the calling context.
    assert secret_refs._cli_install_db.get() is None


def test_agent_parallel_tool_pool_keeps_the_runs_db(home, tmp_path):
    # Parallel-group calls run on a ThreadPoolExecutor; each runs in a copy of
    # the run's context, so a lookup inside the tool still sees the run's db.
    import uuid

    from fabri import ScriptedLLMBackend, ToolRegistry
    from fabri.core.agent import _dispatch_tool_calls
    from fabri.core.llm import ToolCall
    from fabri.tools.manifest_schema import ToolManifest
    from fabri.tools.result import tool_ok

    _store_token(tmp_path / "p", "token-in-pool-333")
    seen: list[str] = []

    def handler(args: dict) -> dict:
        seen.append(resolve_secret("linear:acme"))
        return tool_ok({"done": True})

    reg = ToolRegistry([])
    reg.register_callable(ToolManifest(
        name="spawn_subagent", description="stub", command=[],
        input_schema={"type": "object", "properties": {"parallel_group": {"type": "string"}}},
        output_schema={"type": "object"}, timeout_s=10,
    ), handler)
    calls = [ToolCall(name="spawn_subagent", args={"parallel_group": "g"}, id=f"c{i}") for i in range(3)]
    with secret_refs.cli_install_db(tmp_path / "p" / "installs.db"):
        _dispatch_tool_calls(calls, reg, llm=ScriptedLLMBackend([]), default_task="t", max_subquestions=3,
                             session_id=f"ctx-{uuid.uuid4().hex[:8]}", messages=[], step_num=0)
    assert seen == ["token-in-pool-333"] * 3
