from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fabri.repo import run as repo_run


pytestmark = pytest.mark.unit

LINEAR_TOKEN = "linear-test-token-should-never-leak"
GITHUB_TOKEN = "github-test-token-should-never-leak"
PR_URL = "https://github.com/acme/widgets/pull/17"
COMMENT_URL = "https://linear.app/acme/comment/17"


@dataclass
class _Harness:
    linear: SimpleNamespace
    auth: Mock
    github_provider: Mock
    github_provider_factory: Mock
    git_local: SimpleNamespace
    notify_slack: Mock
    resolve_secret: Mock
    log_event: Mock
    subprocess_calls: list[tuple[list[str], dict[str, object]]]


def _write_crew(tmp_path: Path) -> Path:
    crew_dir = tmp_path / "crew"
    crew_dir.mkdir()
    (crew_dir / "agency.toml").write_text(
        "\n".join(
            [
                "[agency]",
                'name = "repo-fix-crew"',
                'entry = "agent.openai.yaml"',
                'test_cmd = "python -m pytest -q"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    entry = crew_dir / "agent.openai.yaml"
    entry.write_text(
        "\n".join(
            [
                "agent:",
                "  name: repo-fix-manager",
                "tools:",
                "  sandbox_root: __AGENCY_ROOT__/workspace",
                "routing:",
                "  slack:",
                "    default_channel: C-REPO-RUN",
                "repo_run_from: __RUN_FROM__",
                "memory_collection: __AGENCY_SLUG__",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (crew_dir / "specialist.yaml").write_text(
        "agent:\n  name: repo-fix-specialist\n",
        encoding="utf-8",
    )
    return entry


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verifier_returncode: int,
) -> _Harness:
    monkeypatch.delenv("FABRI_SLACK_CHANNEL", raising=False)
    issue = {
        "id": "linear-internal-id",
        "identifier": "FAB-42",
        "title": "Repair the widget",
        "description": "Make the widget return the expected value.",
        "url": "https://linear.app/acme/issue/FAB-42",
        "state": "In Progress",
    }
    linear_namespace = SimpleNamespace(
        fetch_issue=Mock(return_value=issue),
        comment_issue=Mock(return_value=COMMENT_URL),
        LinearError=RuntimeError,
    )

    auth = Mock()
    auth.get_token.return_value = GITHUB_TOKEN
    build_github_auth = Mock(return_value=auth)

    github_provider = Mock()
    github_provider.open_or_update_pr.return_value = PR_URL
    github_provider_factory = Mock(return_value=github_provider)

    git_local_namespace = SimpleNamespace(
        has_changes=Mock(return_value=True),
        commit_and_push_all=Mock(return_value="fabri/FAB-42"),
    )
    slack_notify = Mock(
        return_value={
            "ok": True,
            "result": {
                "ts": "1712345678.900",
                "channel": "C-REPO-RUN",
                "permalink": "https://slack.example/archives/C-REPO-RUN/p1712345678900",
            },
        }
    )

    def resolve_secret(ref: str, store: object | None = None) -> str:
        assert ref == "linear:default"
        assert store is not None
        return LINEAR_TOKEN

    resolve_secret_mock = Mock(side_effect=resolve_secret)
    log_event_mock = Mock()
    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_subprocess_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        subprocess_calls.append((command, dict(kwargs)))

        if command[:2] == ["git", "clone"]:
            checkout = Path(command[-1])
            checkout.mkdir(parents=True)
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "cloned\n", "")

        if command and command[0] == "git":
            if "ls-files" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "diff" in command:
                diff = (
                    "diff --git a/widget.py b/widget.py\n"
                    "--- a/widget.py\n"
                    "+++ b/widget.py\n"
                    f"+fixed {LINEAR_TOKEN} {GITHUB_TOKEN}\n"
                )
                return subprocess.CompletedProcess(command, 0, diff, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        if command and command[0] == "fabri":
            return subprocess.CompletedProcess(
                command,
                0,
                f"advisory crew prose: tests passed ({LINEAR_TOKEN})\n",
                "",
            )

        if command[:3] == ["python", "-m", "pytest"]:
            return subprocess.CompletedProcess(
                command,
                verifier_returncode,
                "1 passed\n" if verifier_returncode == 0 else "1 failed\n",
                "" if verifier_returncode == 0 else "assert 1 == 2\n",
            )

        raise AssertionError(f"unexpected subprocess argv: {command!r}")

    monkeypatch.setattr(repo_run, "linear", linear_namespace)
    monkeypatch.setattr(repo_run, "build_github_auth", build_github_auth)
    monkeypatch.setattr(repo_run, "GitHubProvider", github_provider_factory)
    monkeypatch.setattr(repo_run, "git_local", git_local_namespace)
    monkeypatch.setattr(repo_run, "notify_slack", slack_notify)
    monkeypatch.setattr(repo_run, "resolve_secret", resolve_secret_mock)
    monkeypatch.setattr(repo_run.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(repo_run, "log_event", log_event_mock)

    return _Harness(
        linear=linear_namespace,
        auth=auth,
        github_provider=github_provider,
        github_provider_factory=github_provider_factory,
        git_local=git_local_namespace,
        notify_slack=slack_notify,
        resolve_secret=resolve_secret_mock,
        log_event=log_event_mock,
        subprocess_calls=subprocess_calls,
    )


def test_verified_test_failure_stops_all_side_effect_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_crew(tmp_path)
    harness = _install_fakes(monkeypatch, verifier_returncode=1)
    workspace = tmp_path / "workspace"

    result = repo_run.run_repo_flow(
        issue_id="FAB-42",
        repo="acme/widgets",
        base="main",
        config=config,
        test_cmd="python -m pytest -q",
        session_id="repo-run-failed-verifier",
        workspace=workspace,
        store=object(),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result["ok"] is False
    failed_gates = [gate for gate in result["gates"] if not gate["ok"]]
    assert [gate["name"] for gate in failed_gates] == ["verified_tests"]
    assert [gate["name"] for gate in result["gates"]] == [
        "resolve_creds",
        "fetch_issue",
        "clone",
        "setup",
        "agency_run",
        "verified_tests",
    ]
    verified_gate = failed_gates[0]
    assert verified_gate["data"]["returncode"] == 1
    assert verified_gate["data"]["stdout"] == "1 failed\n"
    assert verified_gate["data"]["stderr"] == "assert 1 == 2\n"
    agency_gate = next(
        gate for gate in result["gates"] if gate["name"] == "agency_run"
    )
    assert agency_gate["ok"] is True

    harness.auth.get_token.assert_not_called()
    harness.git_local.commit_and_push_all.assert_not_called()
    harness.github_provider.open_or_update_pr.assert_not_called()
    harness.github_provider_factory.assert_not_called()
    harness.linear.comment_issue.assert_not_called()
    harness.notify_slack.assert_not_called()
    assert harness.log_event.call_count == 6
    logged_events = [call.args[1] for call in harness.log_event.call_args_list]
    assert [event["gate"] for event in logged_events] == [
        "resolve_creds",
        "fetch_issue",
        "clone",
        "setup",
        "agency_run",
        "verified_tests",
    ]
    assert logged_events[-1]["ok"] is False
    assert all("data" not in event for event in logged_events)

    serialized = json.dumps(result)
    assert LINEAR_TOKEN not in serialized
    assert GITHUB_TOKEN not in serialized


def test_happy_path_pushes_idempotent_branch_and_opens_pr_without_token_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_crew(tmp_path)
    harness = _install_fakes(monkeypatch, verifier_returncode=0)
    workspace = tmp_path / "workspace"

    result = repo_run.run_repo_flow(
        issue_id="FAB-42",
        repo="acme/widgets",
        base="main",
        config=config,
        test_cmd="python -m pytest -q",
        session_id="repo-run-happy-path",
        workspace=workspace,
        store=object(),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["pr_url"] == PR_URL
    assert result["linear_comment_url"] == COMMENT_URL
    assert result["slack_ts"] == "1712345678.900"
    assert [gate["name"] for gate in result["gates"]] == [
        "resolve_creds",
        "fetch_issue",
        "clone",
        "setup",
        "agency_run",
        "verified_tests",
        "branch_push",
        "open_pr",
        "comment_linear",
        "notify_slack",
    ]

    harness.auth.get_token.assert_called_once_with()
    harness.git_local.has_changes.assert_called_once()
    harness.git_local.commit_and_push_all.assert_called_once()
    push_kwargs = harness.git_local.commit_and_push_all.call_args.kwargs
    assert push_kwargs["base"] == "main"
    assert push_kwargs["new_branch"] == "fabri/FAB-42"

    harness.github_provider_factory.assert_called_once_with(GITHUB_TOKEN)
    harness.github_provider.open_or_update_pr.assert_called_once()
    pr_call = harness.github_provider.open_or_update_pr.call_args
    assert pr_call.args[0] == "acme/widgets"
    assert pr_call.kwargs == {
        "key": "FAB-42",
        "head": "fabri/FAB-42",
        "base": "main",
    }

    harness.linear.comment_issue.assert_called_once()
    linear_call = harness.linear.comment_issue.call_args
    assert linear_call.args[0] == "FAB-42"
    assert PR_URL in linear_call.args[1]
    assert linear_call.kwargs["token"] == LINEAR_TOKEN

    harness.notify_slack.assert_called_once()
    slack_call = harness.notify_slack.call_args
    assert PR_URL in slack_call.args[0]
    assert slack_call.kwargs["channel"] == "C-REPO-RUN"

    agency_calls = [
        (argv, kwargs)
        for argv, kwargs in harness.subprocess_calls
        if argv and argv[0] == "fabri"
    ]
    assert len(agency_calls) == 1
    agency_argv, agency_kwargs = agency_calls[0]
    assert agency_argv[0:2] == ["fabri", "--config"]
    assert Path(agency_argv[2]).is_file()
    assert agency_argv[3] == "run"
    assert "FAB-42: Repair the widget" in agency_argv[4]
    assert "Make the widget return the expected value." in agency_argv[4]
    assert "https://linear.app/acme/issue/FAB-42" in agency_argv[4]
    assert agency_argv[5:] == ["--session-id", "repo-run-happy-path"]
    agency_env = agency_kwargs["env"]
    assert isinstance(agency_env, dict)
    checkout_dir = Path(agency_env["FABRI_SANDBOX_ROOT_OVERRIDE"])
    assert Path(agency_env["FABRI_HOME"]) == workspace
    assert not workspace.is_relative_to(checkout_dir)
    assert agency_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert Path(agency_kwargs["cwd"]) == checkout_dir
    assert agency_kwargs["capture_output"] is True
    assert agency_kwargs["text"] is True

    verifier_calls = [
        (argv, kwargs)
        for argv, kwargs in harness.subprocess_calls
        if argv[:3] == ["python", "-m", "pytest"]
    ]
    assert len(verifier_calls) == 1
    verifier_argv, verifier_kwargs = verifier_calls[0]
    cache_flag = verifier_argv.index("-p")
    assert verifier_argv[cache_flag + 1] == "no:cacheprovider"
    assert Path(verifier_kwargs["cwd"]) == checkout_dir
    assert verifier_kwargs["capture_output"] is True
    assert verifier_kwargs["text"] is True
    verifier_env = verifier_kwargs["env"]
    assert isinstance(verifier_env, dict)
    assert verifier_env["PYTHONDONTWRITEBYTECODE"] == "1"

    assert harness.log_event.call_count == 10
    logged_events = [call.args[1] for call in harness.log_event.call_args_list]
    assert [event["gate"] for event in logged_events] == [
        gate["name"] for gate in result["gates"]
    ]
    assert all("data" not in event for event in logged_events)

    serialized = json.dumps(result)
    bundle_dir = Path(result["bundle_dir"])
    assert bundle_dir.name == "repo-run-2026-01-01"
    assert bundle_dir.is_dir()
    assert (bundle_dir / "trace.jsonl").is_file()
    trace_path_text = (bundle_dir / "trace_path.txt").read_text(
        encoding="utf-8"
    ).strip()
    assert trace_path_text == str(bundle_dir / "trace.jsonl")
    bundle_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(bundle_dir.rglob("*"))
        if path.is_file()
    )
    assert LINEAR_TOKEN not in serialized
    assert GITHUB_TOKEN not in serialized
    assert LINEAR_TOKEN not in bundle_text
    assert GITHUB_TOKEN not in bundle_text
    assert PR_URL in bundle_text
    assert COMMENT_URL in bundle_text
    assert "1712345678.900" in bundle_text
    assert "https://slack.example/archives/C-REPO-RUN/p1712345678900" in bundle_text
    assert "diff --git a/widget.py b/widget.py" in bundle_text


# --- multi-tenant Linear token selection ------------------------------------

def _stub_gh_auth(monkeypatch):
    monkeypatch.setattr(
        repo_run, "build_github_auth",
        lambda store, repo=None: SimpleNamespace(get_token=lambda: GITHUB_TOKEN),
    )


def _record_resolve(monkeypatch, seen, returns):
    def _resolve(ref, store=None):
        seen["ref"] = ref
        return returns
    monkeypatch.setattr(repo_run, "resolve_secret", _resolve)


def test_resolve_creds_uses_per_workspace_linear_token(monkeypatch):
    seen = {}
    _record_resolve(monkeypatch, seen, "lin-ws-token")
    _stub_gh_auth(monkeypatch)

    token, _ = repo_run._gate_resolve_creds(None, [], "acme/widgets", "ws-9")

    assert seen["ref"] == "linear:ws-9"
    assert token == "lin-ws-token"


def test_resolve_creds_defaults_to_single_tenant_linear_token(monkeypatch):
    seen = {}
    _record_resolve(monkeypatch, seen, "lin-default")
    _stub_gh_auth(monkeypatch)

    token, _ = repo_run._gate_resolve_creds(None, [], "acme/widgets", None)

    assert seen["ref"] == "linear:default"
    assert token == "lin-default"
