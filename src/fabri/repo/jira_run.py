"""Fail-closed orchestration for turning one routed Jira issue into a draft PR."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from fabri.repo import git_local
from fabri.repo.github_auth import build_github_auth
from fabri.repo.jira_issue import (
    JiraClient,
    ProjectRoute,
    jira_branch_name,
    jira_issue_text,
)
from fabri.repo.run import (
    GateResult,
    RepoRunFailed,
    _capture_diff,
    _failed_gate,
    _finalize_result,
    _gate_agency_run,
    _gate_branch_push,
    _gate_clone,
    _gate_open_pr,
    _gate_setup,
    _gate_verified_tests,
    _record_gate,
    _redact,
    _safe_session_id,
    _stable_checkout_dir,
    _validate_git_ref,
)
from fabri.service.surfaces.jira import _adf_document
from fabri.tools.registry import Approver


def _patch_agent_cost(config_path: Path, max_cost_usd: float) -> None:
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RepoRunFailed("agency_run", f"could not set agency cost cap: {exc}") from exc
    if not isinstance(data, dict):
        raise RepoRunFailed("agency_run", "materialized agency config is not a mapping")
    agent = data.get("agent")
    if not isinstance(agent, dict):
        raise RepoRunFailed("agency_run", "materialized agency config has no agent table")
    agent["max_cost_usd"] = max_cost_usd
    try:
        config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        raise RepoRunFailed("agency_run", f"could not write agency cost cap: {exc}") from exc


def _clone_default_branch(checkout_dir: Path, timeout: float) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(checkout_dir),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoRunFailed(
            "branch_push", f"could not inspect clone default branch: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no output").strip()
        raise RepoRunFailed("branch_push", f"could not inspect clone default branch: {detail}")
    branch = completed.stdout.strip()
    branch = branch.removeprefix("origin/")
    if not branch:
        raise RepoRunFailed("branch_push", "clone default branch was empty")
    return branch


def _issue_fields(route: ProjectRoute) -> list[str]:
    fields = ["summary", "description", "labels", "assignee", "status", "creator", "created"]
    if route.acceptance_field:
        fields.append(route.acceptance_field)
    return fields


def _comment_result_gate(result: dict, *, failure: bool) -> GateResult:
    if result.get("ok") is not True:
        detail = str(result.get("error") or "Jira returned ok=false")
        return GateResult("comment_jira", False, detail)
    return GateResult(
        "comment_jira",
        True,
        "commented failure summary on Jira" if failure else "commented draft PR URL on Jira",
    )


def run_jira_flow(
    *,
    issue_key: str,
    route: ProjectRoute,
    jira: JiraClient,
    approver: Approver | None,
    session_id: str | None = None,
    workspace: str | os.PathLike[str] | None = None,
    store: object | None = None,
    now: datetime | None = None,
    timeout: float = 1800.0,
    clone_timeout: float = 120.0,
) -> dict[str, object]:
    """Run the Jira resolver gates and report the outcome on the Jira issue."""

    run_now = now if now is not None else datetime.now(UTC)
    workspace_path = Path(workspace) if workspace is not None else Path.cwd()
    workspace_path = workspace_path.resolve()
    source_config = route.agency.resolve()
    sid = _safe_session_id(session_id)
    gates: list[GateResult] = []
    known_tokens: list[str] = []
    github_auth: object | None = None
    github_token: str | None = None
    issue: dict[str, Any] = {}
    summary = ""
    branch_name = ""
    checkout_dir: Path | None = None
    pr_url: str | None = None
    produced_diff = ""
    fetched = False

    def finish(ok: bool) -> dict[str, object]:
        return _finalize_result(
            ok=ok,
            gates=gates,
            workspace=workspace_path,
            run_now=run_now,
            session_id=sid,
            pr_url=pr_url,
            linear_comment_url=None,
            slack_data=None,
            produced_diff=produced_diff,
            known_tokens=known_tokens,
        )

    def record(gate: GateResult) -> None:
        _record_gate(gates, gate, sid, known_tokens)

    def fail(gate: GateResult) -> dict[str, object]:
        record(gate)
        if fetched and gate.name != "comment_jira":
            detail = _redact(gate.detail, *known_tokens)
            text = (
                f"fabri could not open a PR for {issue_key}: "
                f"{gate.name} failed: {detail}"
            )[:2000]
            try:
                comment_result = jira.add_comment(
                    issue_key,
                    _adf_document(text),
                    approver,
                )
                comment_gate = _comment_result_gate(comment_result, failure=True)
            except Exception as exc:  # noqa: BLE001 -- normalize connector boundary failures
                comment_gate = _failed_gate("comment_jira", exc, known_tokens)
            record(comment_gate)
        return finish(False)

    try:
        missing = [
            name
            for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
            if not os.environ.get(name)
        ]
        if missing:
            raise RepoRunFailed(
                "resolve_creds",
                f"missing Jira credential environment variables: {', '.join(missing)}",
            )
        known_tokens.append(os.environ["JIRA_API_TOKEN"])
        github_auth = build_github_auth(store, repo=route.repo)
        if github_auth is None or not callable(getattr(github_auth, "get_token", None)):
            raise RepoRunFailed(
                "resolve_creds",
                "GitHub auth provider does not expose get_token()",
            )
        record(GateResult("resolve_creds", True, "Jira credentials and GitHub auth resolved"))
    except Exception as exc:
        return fail(_failed_gate("resolve_creds", exc, known_tokens))

    try:
        issue = jira.get_issue(issue_key, _issue_fields(route))
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            raise RepoRunFailed("fetch_issue", "Jira issue has no fields object")
        raw_summary = fields.get("summary")
        if not isinstance(raw_summary, str) or not raw_summary.strip():
            raise RepoRunFailed("fetch_issue", "Jira issue has no summary")
        summary = raw_summary.strip()
        issue["key"] = issue_key
        issue["url"] = f"{os.environ['JIRA_URL'].rstrip('/')}/browse/{issue_key}"
        if route.acceptance_field:
            issue["_acceptance_field"] = route.acceptance_field
        branch_name = jira_branch_name(issue_key, summary)
        fetched = True
        record(
            GateResult(
                "fetch_issue",
                True,
                f"fetched {issue_key}",
                {"identifier": issue_key, "url": issue["url"]},
            )
        )
    except Exception as exc:
        return fail(_failed_gate("fetch_issue", exc, known_tokens))

    try:
        checkout_dir = _stable_checkout_dir(workspace_path, route.repo, issue_key)
        clone_data = _gate_clone(
            repo=route.repo,
            base=route.base,
            workspace=workspace_path,
            checkout_dir=checkout_dir,
            timeout=clone_timeout,
        )
        clone_gate = GateResult(
            "clone",
            True,
            (
                "reused and reset stable checkout"
                if clone_data["reused"]
                else "cloned stable checkout"
            ),
            clone_data,
        )
        record(clone_gate)
    except Exception as exc:
        return fail(_failed_gate("clone", exc, known_tokens))
    assert checkout_dir is not None

    try:
        setup_gate = _gate_setup(route.setup_cmd, checkout_dir, timeout)
    except Exception as exc:
        setup_gate = _failed_gate("setup", exc, known_tokens)
    if not setup_gate.ok:
        return fail(setup_gate)
    record(setup_gate)

    try:
        agency_gate, _ = _gate_agency_run(
            source_config=source_config,
            workspace=workspace_path,
            checkout_dir=checkout_dir,
            repo=route.repo,
            identifier=issue_key,
            issue_text=jira_issue_text(issue),
            session_id=sid,
            timeout=timeout,
            materialize_hook=lambda path: _patch_agent_cost(path, route.max_cost_usd),
        )
    except Exception as exc:
        agency_gate = _failed_gate("agency_run", exc, known_tokens)
    if not agency_gate.ok:
        return fail(agency_gate)
    record(agency_gate)

    try:
        tests_gate, _ = _gate_verified_tests(
            explicit_cmd=route.test_cmd,
            source_config=source_config,
            checkout_dir=checkout_dir,
            timeout=timeout,
        )
    except Exception as exc:
        tests_gate = _failed_gate("verified_tests", exc, known_tokens)
    if not tests_gate.ok:
        return fail(tests_gate)
    record(tests_gate)

    try:
        default_branch = _clone_default_branch(checkout_dir, clone_timeout)
        if branch_name == route.base or branch_name == default_branch:
            raise RepoRunFailed(
                "branch_push",
                f"refusing to push protected branch {branch_name!r}",
            )
        _validate_git_ref(branch_name, gate="branch_push")
        if not git_local.has_changes(checkout_dir):
            branch_gate = GateResult(
                "branch_push",
                False,
                "no checkout changes to push; PR was not opened",
                {"branch": None, "pushed": False},
            )
            pushed_branch = None
        else:
            produced_diff = _capture_diff(checkout_dir, clone_timeout)
            branch_gate, pushed_branch, github_token = _gate_branch_push(
                checkout_dir=checkout_dir,
                repo=route.repo,
                base=route.base,
                branch_name=branch_name,
                commit_msg=f"{issue_key}: {summary}",
                github_auth=github_auth,
                known_tokens=known_tokens,
                force=False,
                unique_branch=True,
            )
    except Exception as exc:
        detail = _failed_gate("branch_push", exc, known_tokens).detail
        branch_gate = GateResult("branch_push", False, detail)
        pushed_branch = None
    if not branch_gate.ok or pushed_branch is None:
        return fail(branch_gate)
    record(branch_gate)

    try:
        if github_token is None:
            raise RepoRunFailed("open_pr", "GitHub token was not available")
        pr_gate, pr_url = _gate_open_pr(
            token=github_token,
            repo=route.repo,
            base=route.base,
            branch_name=pushed_branch,
            identifier=issue_key,
            issue={"title": summary, "url": str(issue["url"])},
            body=(
                f"Automated change for [{issue_key}]({issue['url']}).\n\n"
                "The authoritative repository test gate passed before this draft PR was opened."
            ),
        )
    except Exception as exc:
        pr_gate = _failed_gate("open_pr", exc, known_tokens)
    if not pr_gate.ok:
        return fail(pr_gate)
    record(pr_gate)

    try:
        if pr_url is None:
            raise RepoRunFailed("comment_jira", "PR URL was not available")
        comment_result = jira.add_comment(
            issue_key,
            _adf_document(f"Draft PR: {pr_url}"),
            approver,
        )
        comment_gate = _comment_result_gate(comment_result, failure=False)
    except Exception as exc:  # noqa: BLE001 -- normalize connector boundary failures
        comment_gate = _failed_gate("comment_jira", exc, known_tokens)
    record(comment_gate)
    if not comment_gate.ok:
        return finish(False)
    return finish(True)
