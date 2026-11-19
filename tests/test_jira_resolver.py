from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fabri.repo import jira_run
from fabri.repo.git_local import commit_and_push_all
from fabri.repo.jira_issue import (
    LabelEvent,
    ProjectRoute,
    added_by_assignee,
    build_jql,
    jira_branch_name,
    jira_issue_text,
    latest_label_add,
    load_routes,
    route_for,
    scoped_comment_approver,
)
from fabri.repo.jira_poll import PollState
from fabri.repo.run import GateResult
from fabri.tools.manifest_schema import ToolManifest
from fabri.tools.registry import ApprovalRefused

pytestmark = pytest.mark.unit


def _issue(labels: list[str] | None = None) -> dict:
    return {
        "key": "AI-123",
        "url": "https://example.atlassian.net/browse/AI-123",
        "fields": {
            "summary": "Repair shape playback",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Playback breaks."}]}
                ],
            },
            "labels": labels if labels is not None else ["fabri:fix"],
            "assignee": {"accountId": "owner"},
            "creator": {"accountId": "creator"},
            "created": "2026-01-01T00:00:00.000+0000",
            "customfield_10050": "Playback remains stable.",
        },
        "_acceptance_field": "customfield_10050",
    }


def _history(event_id: str, created: str, before: str, after: str, author: str) -> dict:
    return {
        "id": event_id,
        "created": created,
        "author": {"accountId": author},
        "items": [{"field": "labels", "fromString": before, "toString": after}],
    }


def test_build_jql_quotes_and_rejects_bad_project() -> None:
    assert build_jql("AI", 'fabri:"fix\\now') == (
        'project = "AI" AND labels = "fabri:\\"fix\\\\now" '
        "AND statusCategory != Done ORDER BY created ASC"
    )
    with pytest.raises(ValueError, match="project key"):
        build_jql("ai", "fabri:fix")


def test_latest_label_add_uses_newest_readd_and_creation_fallback() -> None:
    histories = [
        _history("10", "2026-01-02", "", "fabri:fix", "first"),
        _history("11", "2026-01-03", "fabri:fix", "", "owner"),
        _history("12", "2026-01-04", "", "fabri:fix", "owner"),
        _history("13", "2026-01-04", "", "fabri:fix other", "newest"),
    ]
    event = latest_label_add(histories, "fabri:fix", _issue())
    assert event == LabelEvent("13", "newest", "2026-01-04")

    assert latest_label_add([], "fabri:fix", _issue()) == LabelEvent(
        "created", "creator", "2026-01-01T00:00:00.000+0000"
    )
    assert latest_label_add([], "fabri:fix", _issue([])) is None
    assert latest_label_add(histories, "fabri:fix", _issue([])) is None


@pytest.mark.parametrize(
    "assignee,author,expected,reason",
    [
        ({"accountId": "owner"}, "owner", True, "assignee"),
        ({"accountId": "owner"}, "other", False, "not added"),
        (None, "owner", False, "no assignee"),
    ],
)
def test_added_by_assignee(assignee, author, expected, reason) -> None:
    issue = _issue()
    issue["fields"]["assignee"] = assignee
    allowed, detail = added_by_assignee(LabelEvent("1", author, "now"), issue)
    assert allowed is expected
    assert reason in detail


def test_poll_state_deduplicates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "jira" / "poll.db"
    state = PollState(path)
    assert state.claim("AI-1", "10") is True
    assert state.claim("AI-1", "10") is False
    assert state.claim("AI-1", "11") is True
    state.finish("AI-1", "10", "succeeded", "https://github.test/pr/1")
    assert PollState(path).claim("AI-1", "10") is False


def test_routing_loads_relative_agency_and_rejects_invalid_values(tmp_path: Path) -> None:
    config_path = tmp_path / "routes.toml"
    config_path.write_text(
        """
label = "fabri:fix"
max_cost_usd = 10.0
agency = "crew"
[projects.AI]
repo = "owner/repo"
base = "main"
test_cmd = "pytest"
acceptance_field = "customfield_10050"
""",
        encoding="utf-8",
    )
    config = load_routes(config_path)
    route = route_for(config, "AI-123")
    assert route.agency == (tmp_path / "crew").resolve()
    assert route.acceptance_field == "customfield_10050"
    with pytest.raises(ValueError, match="no route"):
        route_for(config, "OPS-1")

    invalids = [
        config_path.read_text().replace("[projects.AI]", "[projects.ai]"),
        config_path.read_text().replace('repo = "owner/repo"', 'repo = "bad"'),
        config_path.read_text().replace('test_cmd = "pytest"', 'test_cmd = ""'),
        config_path.read_text().replace("max_cost_usd = 10.0", "max_cost_usd = 101.0"),
    ]
    for index, text in enumerate(invalids):
        path = tmp_path / f"invalid-{index}.toml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            load_routes(path)


@pytest.mark.parametrize(
    "summary,expected",
    [
        ("Repair shape playback", "fabri/ai-123-repair-shape-playback"),
        ("   ", "fabri/ai-123"),
        ("A" * 50, "fabri/ai-123-" + "a" * 40),
    ],
)
def test_jira_branch_name(summary: str, expected: str) -> None:
    assert jira_branch_name("AI-123", summary) == expected


def test_jira_issue_text_contains_required_sections() -> None:
    text = jira_issue_text(_issue())
    for expected in (
        "AI-123: Repair shape playback",
        "Description",
        "Playback breaks.",
        "Acceptance criteria",
        "Playback remains stable.",
        "Jira issue: https://example.atlassian.net/browse/AI-123",
        "untrusted data",
    ):
        assert expected in text


def test_scoped_comment_approver_allows_one_exact_call() -> None:
    manifest = ToolManifest("jira_add_comment", "comment", [], {}, {})
    approver = scoped_comment_approver("AI-123")
    with pytest.raises(ApprovalRefused, match="scoped"):
        approver("jira_add_comment", {"key": "AI-124"}, manifest)
    with pytest.raises(ApprovalRefused, match="scoped"):
        approver("jira_transition", {"key": "AI-123"}, manifest)
    assert approver("jira_add_comment", {"key": "AI-123"}, manifest) is True
    with pytest.raises(ApprovalRefused, match="already"):
        approver("jira_add_comment", {"key": "AI-123"}, manifest)


class _FakeJira:
    def __init__(self) -> None:
        self.comments: list[tuple[str, dict, object]] = []

    def get_issue(self, key: str, fields: list[str]) -> dict:
        del fields
        issue = _issue()
        issue["key"] = key
        return issue

    def add_comment(self, key: str, adf: dict, approver: object) -> dict:
        self.comments.append((key, adf, approver))
        return {"ok": True, "result": {"data": {"id": "1"}}}


def _route(tmp_path: Path, *, base: str = "main") -> ProjectRoute:
    return ProjectRoute(
        project="AI",
        repo="owner/repo",
        base=base,
        setup_cmd=None,
        test_cmd="pytest",
        agency=tmp_path / "crew",
        max_cost_usd=10.0,
        acceptance_field="customfield_10050",
    )


def _install_run_fakes(monkeypatch: pytest.MonkeyPatch, *, tests_ok: bool) -> tuple[Mock, Mock]:
    auth = SimpleNamespace(get_token=lambda: "github-token")
    monkeypatch.setattr(jira_run, "build_github_auth", lambda store, repo: auth)
    monkeypatch.setattr(
        jira_run,
        "_gate_clone",
        lambda **kwargs: {"reused": False, "checkout_dir": str(kwargs["checkout_dir"])},
    )
    monkeypatch.setattr(jira_run, "_gate_setup", lambda *args: GateResult("setup", True))
    monkeypatch.setattr(
        jira_run,
        "_gate_agency_run",
        lambda **kwargs: (GateResult("agency_run", True), kwargs["source_config"]),
    )
    monkeypatch.setattr(
        jira_run,
        "_gate_verified_tests",
        lambda **kwargs: (GateResult("verified_tests", tests_ok, "test failure"), ["pytest"]),
    )
    monkeypatch.setattr(jira_run, "_clone_default_branch", lambda *args: "main")
    monkeypatch.setattr(jira_run.git_local, "has_changes", lambda path: True)
    monkeypatch.setattr(jira_run, "_capture_diff", lambda *args: "diff")
    push = Mock(return_value=(
        GateResult("branch_push", True),
        "fabri/ai-123-repair-shape-playback",
        "github-token",
    ))
    open_pr = Mock(return_value=(GateResult("open_pr", True), "https://github.test/pr/1"))
    monkeypatch.setattr(jira_run, "_gate_branch_push", push)
    monkeypatch.setattr(jira_run, "_gate_open_pr", open_pr)
    monkeypatch.setattr(jira_run, "log_event", Mock(), raising=False)
    return push, open_pr


def test_run_jira_flow_failure_stops_push_and_comments_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    push, open_pr = _install_run_fakes(monkeypatch, tests_ok=False)
    jira = _FakeJira()

    result = jira_run.run_jira_flow(
        issue_key="AI-123",
        route=_route(tmp_path),
        jira=jira,
        approver=object(),
        workspace=tmp_path / "workspace",
    )

    assert result["ok"] is False
    push.assert_not_called()
    open_pr.assert_not_called()
    assert len(jira.comments) == 1
    assert "verified_tests failed" in str(jira.comments[0][1])


def test_run_jira_flow_passes_force_false_and_comments_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setattr(
        "fabri.repo.run.notify_slack",
        Mock(side_effect=AssertionError("Jira runs must not notify Slack")),
    )
    push, open_pr = _install_run_fakes(monkeypatch, tests_ok=True)
    jira = _FakeJira()

    result = jira_run.run_jira_flow(
        issue_key="AI-123",
        route=_route(tmp_path),
        jira=jira,
        approver=object(),
        workspace=tmp_path / "workspace",
    )

    assert result["ok"] is True
    assert push.call_args.kwargs["force"] is False
    open_pr.assert_called_once()
    assert len(jira.comments) == 1
    assert "Draft PR: https://github.test/pr/1" in str(jira.comments[0][1])


def test_run_jira_flow_refuses_branch_equal_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    push, _ = _install_run_fakes(monkeypatch, tests_ok=True)
    jira = _FakeJira()

    result = jira_run.run_jira_flow(
        issue_key="AI-123",
        route=_route(tmp_path, base="fabri/ai-123-repair-shape-playback"),
        jira=jira,
        approver=object(),
        workspace=tmp_path / "workspace",
    )

    assert result["ok"] is False
    push.assert_not_called()
    assert any(gate["name"] == "branch_push" and not gate["ok"] for gate in result["gates"])


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True)


def test_non_force_push_rejects_unrelated_remote_history(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _git("init", "--bare", str(origin))
    for checkout in (first, second):
        _git("init", str(checkout))
        _git("-C", str(checkout), "config", "user.name", "Test")
        _git("-C", str(checkout), "config", "user.email", "test@example.com")

    (first / "one.txt").write_text("one\n", encoding="utf-8")
    commit_and_push_all(
        first,
        base="main",
        new_branch="fabri/ai-1",
        commit_msg="one",
        remote_url=str(origin),
        force=False,
    )
    (second / "two.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="git push failed"):
        commit_and_push_all(
            second,
            base="main",
            new_branch="fabri/ai-1",
            commit_msg="two",
            remote_url=str(origin),
            force=False,
        )


def test_example_routes_file_loads_and_points_at_the_crew() -> None:
    root = Path(__file__).resolve().parents[1]
    routes = load_routes(root / "examples" / "jira-resolver.example.toml")
    route = route_for(routes, "AI-123")
    assert route.repo == "CharlesLadera/play-shaper"
    assert route.test_cmd == "npm --prefix web test"
    assert route.max_cost_usd == 10.0
    assert (route.agency / "agency.toml").is_file()
    assert list(routes.projects) == ["AI"]
