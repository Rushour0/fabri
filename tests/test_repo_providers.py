"""Offline tests for the three repository provider adapters."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from fabri import cli
from fabri.repo import bitbucket, gitlab, github
from fabri.repo.base import detect_provider, push_branch_with_url


def test_detect_provider_from_ci_environment() -> None:
    assert detect_provider(None, {"GITHUB_REPOSITORY": "acme/widget"}) == "github"
    assert detect_provider(None, {"GITLAB_CI": "true"}) == "gitlab"
    assert detect_provider(None, {"BITBUCKET_REPO_FULL_NAME": "acme/widget"}) == "bitbucket"
    assert detect_provider("acme/widget", {}) == "github"


def test_github_issue_and_pr_payloads_and_dedup(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None, timeout=30):
        calls.append((method, url, payload))
        if method == "GET" and "/issues" in url:
            return 200, []
        if method == "GET":
            return 200, []
        return 201, {"html_url": "https://github.com/acme/widget/pull/3" if "/pulls" in url else "https://github.com/acme/widget/issues/2"}

    monkeypatch.setattr(github, "_http", fake_http)
    provider = github.GitHubProvider("token")
    provider.open_or_update_issue("acme/widget", "Issue", "Body", "daily", ["fabri"])
    provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main")
    assert calls[0][1].startswith("https://api.github.com/repos/acme/widget/issues?")
    assert calls[1][2] == {"title": "Issue", "body": "Body\n\n<!-- fabri:repo:daily -->", "labels": ["fabri"]}
    assert calls[3][2]["draft"] is True
    assert calls[3][2]["head"] == "feature"

    calls.clear()
    def github_dedup(method, url, token, payload=None, timeout=30):
        calls.append((method, url, payload))
        return (200, [{"number": 5, "html_url": "https://x", "body": "<!-- fabri:repo:open-pr:agent -->"}]) if method == "GET" else (200, {"html_url": "https://x"})
    monkeypatch.setattr(github, "_http", github_dedup)
    assert provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main") == "https://x"
    assert [call[0] for call in calls] == ["GET", "PATCH"]


def test_gitlab_urls_payloads_and_dedup(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return 200, []
        return 201, {"web_url": "https://gitlab.com/acme/widget/-/issues/1"}

    monkeypatch.setattr(gitlab, "_http", fake_http)
    provider = gitlab.GitLabProvider("token")
    provider.open_or_update_issue("acme/widget", "Issue", "Body", "daily", ["fabri"])
    assert calls[0][1] == "https://gitlab.com/api/v4/projects/acme%2Fwidget/issues?state=opened"
    assert calls[1][2] == {"title": "Issue", "description": "Body\n\n<!-- fabri:repo:daily -->", "labels": "fabri"}
    provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main")
    assert calls[3][1] == "https://gitlab.com/api/v4/projects/acme%2Fwidget/merge_requests"
    assert calls[3][2]["source_branch"] == "feature"
    assert calls[3][2]["draft"] is True

    calls.clear()
    def gitlab_dedup(method, url, token, payload=None):
        calls.append((method, url, payload))
        return (200, [{"iid": 9, "web_url": "https://x", "description": "<!-- fabri:repo:open-pr:agent -->"}]) if method == "GET" else (200, {"web_url": "https://x"})
    monkeypatch.setattr(gitlab, "_http", gitlab_dedup)
    assert provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main") == "https://x"
    assert [call[0] for call in calls] == ["GET", "PUT"]


def test_bitbucket_urls_payloads_and_dedup(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return 200, {"values": []}
        return 201, {"links": {"html": {"href": "https://bitbucket.org/acme/widget/issues/1"}}}

    monkeypatch.setattr(bitbucket, "_http", fake_http)
    provider = bitbucket.BitbucketProvider("token")
    provider.open_or_update_issue("acme/widget", "Issue", "Body", "daily", ["fabri"])
    assert calls[0][1] == "https://api.bitbucket.org/2.0/repositories/acme/widget/issues?state=new"
    assert calls[1][2] == {"title": "Issue", "content": {"raw": "Body\n\n<!-- fabri:repo:daily -->"}}
    provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main")
    assert calls[3][1] == "https://api.bitbucket.org/2.0/repositories/acme/widget/pullrequests"
    assert calls[3][2]["source"]["branch"]["name"] == "feature"
    assert calls[3][2]["draft"] is True

    calls.clear()
    def bitbucket_dedup(method, url, token, payload=None):
        calls.append((method, url, payload))
        return (200, {"values": [{"id": 4, "description": "<!-- fabri:repo:open-pr:agent -->"}]}) if method == "GET" else (200, {"links": {"html": {"href": "https://x"}}})
    monkeypatch.setattr(bitbucket, "_http", bitbucket_dedup)
    assert provider.open_or_update_pr("acme/widget", "PR", "Body", "open-pr:agent", "feature", "main") == "https://x"
    assert [call[0] for call in calls] == ["GET", "PUT"]


def test_apply_learned_block_is_idempotent_and_replaces() -> None:
    source = "agent:\n  name: scout\n  system_prompt: |\n    Be useful.\nmemory: {}\n"
    once = cli.apply_learned_block(source, ["Check assumptions."])
    assert cli.apply_learned_block(once, ["Check assumptions."]) == once
    replaced = cli.apply_learned_block(once, ["Use small commits."])
    assert replaced.count("# --- fabri: learned from prior runs (auto-proposed) ---") == 1
    assert "Use small commits." in replaced
    assert "Check assumptions." not in replaced


def test_push_branch_uses_a_temp_clone_and_expected_git_order(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, check):
        calls.append(command)

    monkeypatch.setattr("fabri.repo.base.subprocess.run", fake_run)
    push_branch_with_url("https://token@example.test/acme/widget.git", "main", "fabri/self-improve", "agent.yaml", "agent: {}\n", "message")
    assert [command[1] for command in calls] == ["clone", "-C", "-C", "-C", "-C"]
    assert calls[1][-2:] == ["fabri/self-improve", "origin/main"]
    assert calls[3][-2:] == ["-m", "message"]
    assert calls[4][-2:] == ["origin", "fabri/self-improve"]
    assert not (tmp_path / "agent.yaml").exists()


def test_open_pr_delegates_only_to_provider_and_leaves_config_untouched(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("agent:\n  name: scout\n  system_prompt: |\n    Be useful.\nmemory: {}\n")
    original = config_path.read_text()

    class Store:
        def iterate(self, kind, limit):
            return [type("Guideline", (), {"text": "Check assumptions."})()]

    class Provider:
        def push_branch(self, *args):
            assert args[3] == "agent.yaml"
        def open_or_update_pr(self, *args):
            return "https://example.test/pr/1"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda path: {"agent": {"name": "scout"}, "memory": {}})
    monkeypatch.setattr(cli, "_open_store", lambda memory: Store())
    monkeypatch.setattr(cli, "get_provider", lambda name, token: Provider())
    cli.cmd_repo_open_pr(Namespace(config="agent.yaml", repo="acme/widget", token="token", provider="github", kind="strategic", limit=10, base="main", branch="fabri/self-improve"))
    assert config_path.read_text() == original
    assert capsys.readouterr().out == "https://example.test/pr/1\n"
