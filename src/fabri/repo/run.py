"""Fail-closed orchestration for turning a Linear issue into a GitHub PR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

from fabri import agency_scaffold
from fabri.integrations import linear
from fabri.orchestrator.traces import log_event
from fabri.repo import git_local
from fabri.repo.base import token_url
from fabri.repo.github import GitHubProvider
from fabri.repo.github_auth import build_github_auth
from fabri.service.repo_run_notify import notify_slack
from fabri.tools.secret_refs import resolve_secret


class RepoRunFailed(Exception):
    """A named repo-run gate failed."""

    def __init__(self, gate: str, detail: str) -> None:
        self.gate = gate
        self.detail = detail
        super().__init__(f"{gate}: {detail}")


@dataclass
class GateResult:
    """The serializable outcome of one attempted gate."""

    name: str
    ok: bool
    detail: str = ""
    data: dict | None = None


def run_repo_flow(
    *,
    issue_id: str,
    repo: str,
    base: str = "main",
    config: str | os.PathLike[str],
    test_cmd: str | None = None,
    setup_cmd: str | None = None,
    session_id: str | None = None,
    workspace: str | os.PathLike[str] | None = None,
    store: object | None = None,
    now: datetime | None = None,
    timeout: float = 1800.0,
    clone_timeout: float = 120.0,
) -> dict[str, object]:
    """Run the ten repo gates and return a JSON-serializable result.

    Only gates that are actually attempted are recorded. In particular, no
    branch, PR, Linear comment, or Slack notification is attempted unless the
    verifier subprocess itself returned zero.
    """

    run_now = now if now is not None else datetime.now(timezone.utc)
    workspace_path = Path(workspace) if workspace is not None else Path.cwd()
    workspace_path = workspace_path.resolve()
    source_config = Path(config).resolve()
    sid = _safe_session_id(session_id)

    gates: list[GateResult] = []
    known_tokens: list[str] = []
    linear_token = ""
    github_auth: object | None = None
    github_token: str | None = None
    issue: dict[str, str] = {}
    identifier = ""
    branch_name = ""
    checkout_dir: Path | None = None
    pr_url: str | None = None
    linear_comment_url: str | None = None
    slack_data: dict[str, object] | None = None
    produced_diff = ""

    def finish(ok: bool) -> dict[str, object]:
        return _finalize_result(
            ok=ok,
            gates=gates,
            workspace=workspace_path,
            run_now=run_now,
            session_id=sid,
            pr_url=pr_url,
            linear_comment_url=linear_comment_url,
            slack_data=slack_data,
            produced_diff=produced_diff,
            known_tokens=known_tokens,
        )

    # 1. resolve_creds
    try:
        linear_token, github_auth = _gate_resolve_creds(store, known_tokens, repo)
        gate = GateResult(
            "resolve_creds",
            True,
            "Linear credential and GitHub auth provider resolved",
        )
    except Exception as exc:
        gate = _failed_gate("resolve_creds", exc, known_tokens)
        _record_gate(gates, gate, sid, known_tokens)
        return finish(False)
    _record_gate(gates, gate, sid, known_tokens)

    # 2. fetch_issue
    try:
        issue = _gate_fetch_issue(issue_id, linear_token)
        identifier = issue["identifier"]
        branch_name = f"fabri/{identifier}"
        _validate_git_ref(branch_name, gate="fetch_issue")
        gate = GateResult(
            "fetch_issue",
            True,
            f"fetched {identifier}",
            {"identifier": identifier, "url": issue["url"]},
        )
    except Exception as exc:
        gate = _failed_gate("fetch_issue", exc, known_tokens)
        _record_gate(gates, gate, sid, known_tokens)
        return finish(False)
    _record_gate(gates, gate, sid, known_tokens)

    # 3. clone
    try:
        checkout_dir = _stable_checkout_dir(workspace_path, repo, identifier)
        clone_data = _gate_clone(
            repo=repo,
            base=base,
            workspace=workspace_path,
            checkout_dir=checkout_dir,
            timeout=clone_timeout,
        )
        gate = GateResult(
            "clone",
            True,
            "reused and reset stable checkout"
            if clone_data["reused"]
            else "cloned stable checkout",
            clone_data,
        )
    except Exception as exc:
        gate = _failed_gate("clone", exc, known_tokens)
        _record_gate(gates, gate, sid, known_tokens)
        return finish(False)
    _record_gate(gates, gate, sid, known_tokens)

    # The clone gate either produced this path or returned above.
    assert checkout_dir is not None

    # 4. setup
    try:
        gate = _gate_setup(setup_cmd, checkout_dir, timeout)
    except Exception as exc:
        gate = _failed_gate("setup", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    # 5. agency_run
    try:
        gate, _ = _gate_agency_run(
            source_config=source_config,
            workspace=workspace_path,
            checkout_dir=checkout_dir,
            repo=repo,
            identifier=identifier,
            issue_text=_issue_text(issue),
            session_id=sid,
            timeout=timeout,
        )
    except Exception as exc:
        gate = _failed_gate("agency_run", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    # 6. verified_tests -- this subprocess's captured return code is truth.
    try:
        gate, _ = _gate_verified_tests(
            explicit_cmd=test_cmd,
            source_config=source_config,
            checkout_dir=checkout_dir,
            timeout=timeout,
        )
    except Exception as exc:
        gate = _failed_gate("verified_tests", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    # 7. branch_push -- reachable only after the verifier returned zero.
    try:
        if not git_local.has_changes(checkout_dir):
            gate = GateResult(
                "branch_push",
                False,
                "no checkout changes to push; PR was not opened",
                {"branch": None, "pushed": False},
            )
            pushed_branch = None
        else:
            # Assign this before auth and push so a later failure bundle still
            # contains the actual change the agency produced.
            produced_diff = _capture_diff(checkout_dir, clone_timeout)
            gate, pushed_branch, github_token = _gate_branch_push(
                checkout_dir=checkout_dir,
                repo=repo,
                base=base,
                branch_name=branch_name,
                commit_msg=f"{identifier}: {issue['title']}",
                github_auth=github_auth,
                known_tokens=known_tokens,
            )
    except Exception as exc:
        gate = _failed_gate("branch_push", exc, known_tokens)
        pushed_branch = None
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok or pushed_branch is None:
        return finish(False)

    # 8. open_pr
    try:
        if github_token is None:
            raise RepoRunFailed("open_pr", "GitHub token was not available")
        gate, pr_url = _gate_open_pr(
            token=github_token,
            repo=repo,
            base=base,
            branch_name=branch_name,
            identifier=identifier,
            issue=issue,
        )
    except Exception as exc:
        gate = _failed_gate("open_pr", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    # 9. comment_linear
    try:
        if pr_url is None:
            raise RepoRunFailed("comment_linear", "PR URL was not available")
        gate, linear_comment_url = _gate_comment_linear(
            issue_id=issue_id,
            issue=issue,
            identifier=identifier,
            pr_url=pr_url,
            token=linear_token,
        )
    except Exception as exc:
        gate = _failed_gate("comment_linear", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    # 10. notify_slack
    try:
        if pr_url is None:
            raise RepoRunFailed("notify_slack", "PR URL was not available")
        gate, slack_data = _gate_notify_slack(
            source_config=source_config,
            identifier=identifier,
            pr_url=pr_url,
        )
    except Exception as exc:
        gate = _failed_gate("notify_slack", exc, known_tokens)
    _record_gate(gates, gate, sid, known_tokens)
    if not gate.ok:
        return finish(False)

    return finish(True)


def _redact(text: object, *tokens: str) -> str:
    """Redact known credentials and common credential-bearing syntax."""

    redacted = str(text)
    for token in sorted({token for token in tokens if token}, key=len, reverse=True):
        redacted = redacted.replace(token, "***")
        encoded = quote(token, safe="")
        if encoded != token:
            redacted = redacted.replace(encoded, "***")

    redacted = re.sub(
        r"(?i)(https?://)[^/@\s]+@",
        r"\1***@",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(authorization|api[_-]?key|password|secret|token)"
        r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1\2***",
        redacted,
    )
    return redacted


def materialize_crew(
    src_agency_dir: str | os.PathLike[str],
    dest_dir: str | os.PathLike[str],
    run_from: str | os.PathLike[str],
) -> Path:
    """Copy and render an agency package, returning its entry YAML.

    ``src_agency_dir`` may be an agency directory or one YAML file. When a
    file has a sibling ``agency.toml``, the complete sibling package is copied
    so referenced specialist configs and scripts remain available.
    """

    source = Path(src_agency_dir).resolve()
    destination = Path(dest_dir).resolve()
    package_root, entry = _locate_agency_source(source)

    if package_root is None:
        if destination == source.parent or destination.is_symlink():
            raise ValueError("materialized agency destination would overwrite its source")
        if source.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError(f"agency config is not YAML: {source}")
        files = {source.name: source.read_text(encoding="utf-8")}
        readme = ""
        entry = source.name
        slug_source = source.stem
    else:
        if destination == package_root or destination.is_relative_to(package_root):
            raise ValueError("materialized agency destination must be outside its source")
        files, readme = _read_agency_files(package_root)
        slug_source = package_root.name

    entry_path = Path(entry)
    if entry_path.is_absolute() or ".." in entry_path.parts:
        raise ValueError(f"agency entry path is unsafe: {entry!r}")
    if entry_path.as_posix() not in files:
        raise ValueError(f"agency entry does not exist: {entry}")

    agency_scaffold.write_template(
        destination,
        files,
        readme,
        run_from=os.fspath(run_from),
        slug=_slug(slug_source),
    )
    materialized_entry = destination / entry_path
    if not materialized_entry.is_file():
        raise OSError(f"materialized agency entry was not written: {entry}")
    return materialized_entry


def _gate_resolve_creds(
    store: object | None,
    known_tokens: list[str],
    repo: str | None,
) -> tuple[str, object]:
    token = resolve_secret("linear:default", store)
    if not isinstance(token, str) or not token:
        raise RepoRunFailed("resolve_creds", "Linear credential was empty")
    known_tokens.append(token)

    auth = build_github_auth(store, repo=repo)
    if auth is None or not callable(getattr(auth, "get_token", None)):
        raise RepoRunFailed(
            "resolve_creds",
            "GitHub auth provider does not expose get_token()",
        )
    return token, auth


def _gate_fetch_issue(issue_id: str, token: str) -> dict[str, str]:
    raw_issue = linear.fetch_issue(issue_id, token=token)
    if not isinstance(raw_issue, dict):
        raise RepoRunFailed("fetch_issue", "Linear returned a non-object issue")

    identifier = raw_issue.get("identifier")
    title = raw_issue.get("title")
    description = raw_issue.get("description")
    url = raw_issue.get("url")

    if not isinstance(identifier, str) or not identifier.strip():
        raise RepoRunFailed("fetch_issue", "Linear issue has no identifier")
    if not isinstance(title, str) or not title.strip():
        raise RepoRunFailed("fetch_issue", "Linear issue has no title")
    if description is not None and not isinstance(description, str):
        raise RepoRunFailed("fetch_issue", "Linear issue description is invalid")
    if not isinstance(url, str) or not url.strip():
        raise RepoRunFailed("fetch_issue", "Linear issue has no URL")

    return {
        "identifier": identifier.strip(),
        "title": title.strip(),
        "description": description or "",
        "url": url.strip(),
    }


def _gate_clone(
    *,
    repo: str,
    base: str,
    workspace: Path,
    checkout_dir: Path,
    timeout: float,
) -> dict:
    _validate_repo(repo)
    _validate_git_ref(base, gate="clone")
    _ensure_workspace_target(
        checkout_dir,
        workspace,
        gate="clone",
    )
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    remote_url = f"https://github.com/{repo}.git"

    if checkout_dir.exists():
        if (
            checkout_dir.is_symlink()
            or (checkout_dir / ".git").is_symlink()
            or not (checkout_dir / ".git").is_dir()
        ):
            raise RepoRunFailed(
                "clone",
                f"stable checkout exists but is not a git repository: {checkout_dir}",
            )
        commands = [
            [
                "git",
                "-C",
                str(checkout_dir),
                "remote",
                "set-url",
                "origin",
                remote_url,
            ],
            ["git", "-C", str(checkout_dir), "fetch", "--prune", "origin"],
            [
                "git",
                "-C",
                str(checkout_dir),
                "checkout",
                "--force",
                "-B",
                base,
                f"origin/{base}",
            ],
            ["git", "-C", str(checkout_dir), "clean", "-fdx"],
        ]
        outputs: list[subprocess.CompletedProcess[str]] = []
        for command in commands:
            completed = _run_captured(command, timeout=timeout)
            _require_zero("clone", completed)
            outputs.append(completed)
        return {
            "reused": True,
            "checkout_dir": str(checkout_dir),
            "stdout": "\n".join(item.stdout or "" for item in outputs).strip(),
            "stderr": "\n".join(item.stderr or "" for item in outputs).strip(),
        }

    command = [
        "git",
        "clone",
        "--branch",
        base,
        remote_url,
        str(checkout_dir),
    ]
    completed = _run_captured(command, timeout=timeout)
    _require_zero("clone", completed)
    return {
        "reused": False,
        "checkout_dir": str(checkout_dir),
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _gate_setup(
    setup_cmd: str | None,
    checkout_dir: Path,
    timeout: float,
) -> GateResult:
    if setup_cmd is None or not setup_cmd.strip():
        return GateResult(
            "setup",
            True,
            "skipped: no setup command configured",
            {"skipped": True},
        )

    argv = _split_command(setup_cmd, gate="setup")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = _run_captured(
        argv,
        cwd=checkout_dir,
        env=env,
        timeout=timeout,
    )
    data = _completed_summary(completed)
    if completed.returncode != 0:
        return GateResult(
            "setup",
            False,
            f"setup command exited with code {completed.returncode}; "
            "remaining gates were not run",
            data,
        )
    return GateResult("setup", True, "setup command completed", data)


def _gate_agency_run(
    *,
    source_config: Path,
    workspace: Path,
    checkout_dir: Path,
    repo: str,
    identifier: str,
    issue_text: str,
    session_id: str,
    timeout: float,
) -> tuple[GateResult, Path]:
    destination = _stable_agency_dir(workspace, repo, identifier)
    _ensure_workspace_target(destination, workspace, gate="agency_run")
    materialized_config = materialize_crew(
        source_config,
        destination,
        str(checkout_dir),
    )
    argv = [
        "fabri",
        "--config",
        str(materialized_config),
        "run",
        issue_text,
        "--session-id",
        session_id,
    ]
    env = os.environ.copy()
    env.update(
        {
            "FABRI_SANDBOX_ROOT_OVERRIDE": str(checkout_dir),
            "FABRI_HOME": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = _run_captured(
        argv,
        cwd=checkout_dir,
        env=env,
        timeout=timeout,
    )
    data = _completed_summary(completed)
    data["config"] = str(materialized_config)
    if completed.returncode != 0:
        return (
            GateResult(
                "agency_run",
                False,
                f"agency command exited with code {completed.returncode}; "
                "verified tests were not run",
                data,
            ),
            materialized_config,
        )
    return (
        GateResult(
            "agency_run",
            True,
            "agency command completed; crew prose is advisory",
            data,
        ),
        materialized_config,
    )


def _gate_verified_tests(
    *,
    explicit_cmd: str | None,
    source_config: Path,
    checkout_dir: Path,
    timeout: float,
) -> tuple[GateResult, list[str]]:
    resolved_cmd = _resolve_test_cmd(explicit_cmd, source_config)
    argv = _split_command(resolved_cmd, gate="verified_tests")
    argv = _without_pytest_cache(argv)

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = _run_captured(
        argv,
        cwd=checkout_dir,
        env=env,
        timeout=timeout,
    )
    data = _completed_data(completed)
    if completed.returncode != 0:
        return (
            GateResult(
                "verified_tests",
                False,
                f"authoritative test command exited with code "
                f"{completed.returncode}",
                data,
            ),
            argv,
        )
    return (
        GateResult(
            "verified_tests",
            True,
            "authoritative test command returned zero",
            data,
        ),
        argv,
    )


def _gate_branch_push(
    *,
    checkout_dir: Path,
    repo: str,
    base: str,
    branch_name: str,
    commit_msg: str,
    github_auth: object | None,
    known_tokens: list[str],
) -> tuple[GateResult, str | None, str | None]:
    get_token = getattr(github_auth, "get_token", None)
    if not callable(get_token):
        raise RepoRunFailed("branch_push", "GitHub auth provider is unavailable")
    token = get_token()
    if not isinstance(token, str) or not token:
        raise RepoRunFailed("branch_push", "GitHub credential was empty")
    known_tokens.append(token)

    remote_url = token_url("x-access-token", token, "github.com", repo)
    pushed_branch = git_local.commit_and_push_all(
        checkout_dir,
        base=base,
        new_branch=branch_name,
        commit_msg=commit_msg,
        remote_url=remote_url,
    )
    if pushed_branch is None:
        return (
            GateResult(
                "branch_push",
                False,
                "checkout became empty before commit; PR was not opened",
                {"branch": None, "pushed": False},
            ),
            None,
            token,
        )
    if pushed_branch != branch_name:
        raise RepoRunFailed(
            "branch_push",
            f"git pushed unexpected branch {pushed_branch!r}",
        )

    return (
        GateResult(
            "branch_push",
            True,
            f"pushed {branch_name}",
            {"branch": branch_name, "pushed": True},
        ),
        pushed_branch,
        token,
    )


def _gate_open_pr(
    *,
    token: str,
    repo: str,
    base: str,
    branch_name: str,
    identifier: str,
    issue: Mapping[str, str],
) -> tuple[GateResult, str]:
    provider = GitHubProvider(token)
    title = f"{identifier}: {issue['title']}"
    body = (
        f"Automated change for [{identifier}]({issue['url']}).\n\n"
        "The authoritative repository test gate passed before this PR was opened."
    )
    pr_url = provider.open_or_update_pr(
        repo,
        title,
        body,
        key=identifier,
        head=branch_name,
        base=base,
    )
    if not isinstance(pr_url, str) or not pr_url:
        raise RepoRunFailed("open_pr", "GitHub returned no PR URL")
    return (
        GateResult("open_pr", True, "opened or updated idempotent PR", {"pr_url": pr_url}),
        pr_url,
    )


def _gate_comment_linear(
    *,
    issue_id: str,
    issue: Mapping[str, str],
    identifier: str,
    pr_url: str,
    token: str,
) -> tuple[GateResult, str | None]:
    prior_text = issue.get("description", "")
    if pr_url in prior_text:
        return (
            GateResult(
                "comment_linear",
                True,
                "skipped: issue text already contains PR URL",
                {"skipped": True, "pr_url": pr_url},
            ),
            None,
        )

    body = f"Fabri opened or updated a pull request for {identifier}: {pr_url}"
    comment_url = linear.comment_issue(issue_id, body, token=token)
    if not isinstance(comment_url, str) or not comment_url:
        raise RepoRunFailed("comment_linear", "Linear returned no comment URL")
    return (
        GateResult(
            "comment_linear",
            True,
            "commented PR URL on Linear",
            {"comment_url": comment_url},
        ),
        comment_url,
    )


def _gate_notify_slack(
    *,
    source_config: Path,
    identifier: str,
    pr_url: str,
) -> tuple[GateResult, dict[str, object]]:
    channel = _resolve_slack_channel(source_config)
    if not channel:
        raise RepoRunFailed(
            "notify_slack",
            "no Slack channel configured in the agency or FABRI_SLACK_CHANNEL",
        )

    response = notify_slack(
        f"PR opened or updated for {identifier}: {pr_url}",
        channel=channel,
    )
    if not isinstance(response, dict):
        raise RepoRunFailed("notify_slack", "Slack notifier returned a non-object result")
    if response.get("ok") is not True:
        error = response.get("error")
        raise RepoRunFailed(
            "notify_slack",
            str(error) if error else "Slack notifier returned ok=false",
        )

    raw_result = response.get("result")
    if not isinstance(raw_result, dict):
        raise RepoRunFailed("notify_slack", "Slack notifier returned no result object")
    ts = raw_result.get("ts")
    result_channel = raw_result.get("channel", channel)
    permalink = raw_result.get("permalink")
    if not isinstance(ts, str) or not ts:
        raise RepoRunFailed("notify_slack", "Slack notifier returned no message ts")
    if not isinstance(result_channel, str) or not result_channel:
        raise RepoRunFailed("notify_slack", "Slack notifier returned no channel")

    slack_data: dict[str, object] = {
        "ts": ts,
        "channel": result_channel,
        "permalink": permalink if isinstance(permalink, str) else None,
    }
    return (
        GateResult(
            "notify_slack",
            True,
            "posted PR notification to Slack",
            dict(slack_data),
        ),
        slack_data,
    )


def _write_results_bundle(
    *,
    bundle_dir: Path,
    result: Mapping[str, object],
    gates: list[GateResult],
    workspace: Path,
    session_id: str,
    slack_data: Mapping[str, object] | None,
    produced_diff: str,
    known_tokens: list[str],
) -> None:
    """Write truthful, redacted run evidence to the deterministic bundle."""

    _ensure_workspace_target(bundle_dir, workspace, gate="results_bundle")
    bundle_dir.mkdir(parents=True, exist_ok=True)
    trace_bundle_path = bundle_dir / "trace.jsonl"
    source_trace_path = workspace / ".fabri" / "traces" / f"{session_id}.jsonl"

    trace_lines: list[str] = []
    seen_gate_names: set[str] = set()
    source_trace_available = (
        source_trace_path.is_file()
        and _is_safe_workspace_path(source_trace_path, workspace)
    )
    if source_trace_available:
        source_text = source_trace_path.read_text(encoding="utf-8", errors="replace")
        trace_lines.extend(_redacted_jsonl_lines(source_text, known_tokens))
        seen_gate_names = _logged_gate_names(source_text)
    for gate in gates:
        if gate.name in seen_gate_names:
            continue
        gate_record = {
            "type": "repo_run_gate_bundle",
            "session_id": session_id,
            **asdict(gate),
        }
        safe_record = _redact_value(gate_record, known_tokens)
        trace_lines.append(json.dumps(safe_record, sort_keys=True))
    trace_text = "\n".join(trace_lines)
    if trace_text:
        trace_text += "\n"
    trace_bundle_path.write_text(trace_text, encoding="utf-8")

    safe_slack = _redact_value(
        dict(slack_data) if slack_data is not None else {
            "ts": None,
            "channel": None,
            "permalink": None,
        },
        known_tokens,
    )
    bundle_record = {
        **result,
        "trace_jsonl": str(trace_bundle_path),
        "source_trace_jsonl": (
            str(source_trace_path) if source_trace_available else None
        ),
        "slack": safe_slack,
    }
    safe_bundle_record = _redact_value(bundle_record, known_tokens)

    (bundle_dir / "result.json").write_text(
        json.dumps(safe_bundle_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "trace_path.txt").write_text(
        f"{_redact(trace_bundle_path, *known_tokens)}\n",
        encoding="utf-8",
    )
    (bundle_dir / "pr_url.txt").write_text(
        _optional_line(result.get("pr_url"), known_tokens),
        encoding="utf-8",
    )
    (bundle_dir / "linear_comment_url.txt").write_text(
        _optional_line(result.get("linear_comment_url"), known_tokens),
        encoding="utf-8",
    )
    (bundle_dir / "slack.json").write_text(
        json.dumps(safe_slack, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "diff.patch").write_text(
        _redact(produced_diff, *known_tokens),
        encoding="utf-8",
    )


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEFAULT_TEST_CMD = "python -m pytest -q -p no:cacheprovider"


def _failed_gate(
    name: str,
    exc: Exception,
    known_tokens: list[str],
) -> GateResult:
    detail = exc.detail if isinstance(exc, RepoRunFailed) else str(exc)
    if not detail:
        detail = type(exc).__name__
    return GateResult(name, False, _redact(detail, *known_tokens))


def _record_gate(
    gates: list[GateResult],
    gate: GateResult,
    session_id: str,
    known_tokens: list[str],
) -> None:
    safe_data = _redact_value(gate.data, known_tokens)
    safe_gate = GateResult(
        name=gate.name,
        ok=gate.ok,
        detail=_redact(gate.detail, *known_tokens),
        data=safe_data if isinstance(safe_data, dict) else None,
    )
    gates.append(safe_gate)

    event: dict[str, object] = {
        "type": "repo_run_gate",
        "gate": safe_gate.name,
        "ok": safe_gate.ok,
        "detail": safe_gate.detail,
    }

    try:
        log_event(session_id, event)
    except Exception as exc:
        # Trace persistence is supplementary to the fail-closed side-effect
        # gates. Keep the failure visible in the bundle without leaking it.
        trace_error = _redact(str(exc), *known_tokens)
        suffix = f"trace logging failed: {trace_error}"
        safe_gate.detail = (
            f"{safe_gate.detail}; {suffix}" if safe_gate.detail else suffix
        )


def _finalize_result(
    *,
    ok: bool,
    gates: list[GateResult],
    workspace: Path,
    run_now: datetime,
    session_id: str,
    pr_url: str | None,
    linear_comment_url: str | None,
    slack_data: Mapping[str, object] | None,
    produced_diff: str,
    known_tokens: list[str],
) -> dict[str, object]:
    bundle_dir = (
        workspace
        / "benchmarks"
        / "results"
        / f"repo-run-{run_now.date().isoformat()}"
    )
    slack_ts_value = slack_data.get("ts") if slack_data is not None else None
    raw_result: dict[str, object] = {
        "ok": ok,
        "gates": [asdict(gate) for gate in gates],
        "pr_url": pr_url,
        "linear_comment_url": linear_comment_url,
        "slack_ts": (
            str(slack_ts_value) if slack_ts_value is not None else None
        ),
        "bundle_dir": str(bundle_dir),
    }
    safe_value = _redact_value(raw_result, known_tokens)
    if not isinstance(safe_value, dict):
        raise RuntimeError("internal repo-run result was not an object")
    safe_result: dict[str, object] = {
        str(key): value for key, value in safe_value.items()
    }

    try:
        _write_results_bundle(
            bundle_dir=bundle_dir,
            result=safe_result,
            gates=gates,
            workspace=workspace,
            session_id=session_id,
            slack_data=slack_data,
            produced_diff=produced_diff,
            known_tokens=known_tokens,
        )
    except Exception as exc:
        # Final artifact writing is a boundary: normalize filesystem and
        # serialization failures instead of leaking an exception or secret.
        safe_result["ok"] = False
        safe_result["bundle_dir"] = None
        safe_result["bundle_error"] = _redact(str(exc), *known_tokens)
    return safe_result


def _safe_session_id(session_id: str | None) -> str:
    if session_id and _SESSION_ID_RE.fullmatch(session_id):
        return session_id
    if session_id:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return f"repo-run-{digest}"
    return f"repo-run-{uuid.uuid4().hex}"


def _stable_checkout_dir(workspace: Path, repo: str, identifier: str) -> Path:
    return (
        workspace
        / ".fabri"
        / "repo-runs"
        / "checkouts"
        / _stable_run_key(repo, identifier)
    )


def _stable_agency_dir(workspace: Path, repo: str, identifier: str) -> Path:
    return (
        workspace
        / ".fabri"
        / "repo-runs"
        / "agencies"
        / _stable_run_key(repo, identifier)
    )


def _stable_run_key(repo: str, identifier: str) -> str:
    readable = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        f"{repo}-{identifier}",
    ).strip("-._")
    readable = readable[:64] or "repo-run"
    digest = hashlib.sha256(
        f"{repo}\0{identifier}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{readable}-{digest}"


def _ensure_workspace_target(
    target: Path,
    workspace: Path,
    *,
    gate: str,
) -> None:
    if not _is_safe_workspace_path(target, workspace):
        raise RepoRunFailed(
            gate,
            f"path escapes the run workspace or crosses a symlink: {target}",
        )


def _is_safe_workspace_path(target: Path, workspace: Path) -> bool:
    root = workspace.resolve()
    absolute_target = target.absolute()
    try:
        relative = absolute_target.relative_to(root)
    except ValueError:
        return False

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    resolved_target = target.resolve()
    return resolved_target == root or resolved_target.is_relative_to(root)


def _validate_repo(repo: str) -> None:
    parts = repo.split("/")
    if (
        len(parts) != 2
        or any(not part for part in parts)
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
        or any(part in {".", ".."} or part.startswith("-") for part in parts)
        or parts[1].lower().endswith(".git")
    ):
        raise RepoRunFailed("clone", "repo must be a GitHub owner/name")


def _validate_git_ref(ref: str, *, gate: str) -> None:
    if (
        not ref
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or ref.endswith((".", "/", ".lock"))
    ):
        raise RepoRunFailed(gate, f"unsafe git ref: {ref!r}")


def _issue_text(issue: Mapping[str, str]) -> str:
    parts = [f"{issue['identifier']}: {issue['title']}"]
    description = issue.get("description", "").strip()
    if description:
        parts.append(description)
    parts.append(f"Linear issue: {issue['url']}")
    return "\n\n".join(parts)


def _run_captured(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if env is None:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _require_zero(
    gate: str,
    completed: subprocess.CompletedProcess[str],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> None:
    if completed.returncode in allowed_returncodes:
        return
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    detail = output or "no output"
    raise RepoRunFailed(
        gate,
        f"command exited with code {completed.returncode}: {detail}",
    )


def _completed_data(
    completed: subprocess.CompletedProcess[str],
) -> dict:
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _completed_summary(
    completed: subprocess.CompletedProcess[str],
) -> dict:
    return {"returncode": completed.returncode}


def _split_command(command: str, *, gate: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RepoRunFailed(gate, f"invalid command: {exc}") from exc
    if not argv:
        raise RepoRunFailed(gate, "command is empty")
    return argv


def _resolve_test_cmd(explicit_cmd: str | None, source_config: Path) -> str:
    if explicit_cmd is not None:
        if not explicit_cmd.strip():
            raise RepoRunFailed("verified_tests", "explicit test command is empty")
        return explicit_cmd

    environment_cmd = os.environ.get("FABRI_REPO_TEST_CMD")
    if environment_cmd:
        return environment_cmd

    agency_toml = (
        source_config / "agency.toml"
        if source_config.is_dir()
        else source_config.parent / "agency.toml"
    )
    if agency_toml.is_file():
        try:
            metadata = tomllib.loads(agency_toml.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RepoRunFailed(
                "verified_tests",
                f"could not read agency test command: {exc}",
            ) from exc
        agency = metadata.get("agency", {})
        if not isinstance(agency, dict):
            raise RepoRunFailed(
                "verified_tests",
                "agency.toml [agency] table is invalid",
            )
        configured_cmd = agency.get("test_cmd")
        if configured_cmd is not None:
            if not isinstance(configured_cmd, str) or not configured_cmd.strip():
                raise RepoRunFailed(
                    "verified_tests",
                    "agency.toml test_cmd must be a non-empty string",
                )
            return configured_cmd

    return _DEFAULT_TEST_CMD


def _without_pytest_cache(argv: list[str]) -> list[str]:
    if not _is_pytest_invocation(argv):
        return argv
    if _has_disabled_cacheprovider(argv):
        return argv
    return [*argv, "-p", "no:cacheprovider"]


def _is_pytest_invocation(argv: list[str]) -> bool:
    candidate = argv
    executable = Path(candidate[0]).name.lower()
    if (
        executable in {"uv", "poetry", "pipenv"}
        and len(candidate) >= 3
        and candidate[1] == "run"
    ):
        candidate = candidate[2:]
        executable = Path(candidate[0]).name.lower()

    if executable in {"pytest", "py.test"}:
        return True
    if not executable.startswith("python"):
        return False
    return any(
        candidate[index : index + 2] == ["-m", "pytest"]
        for index in range(1, len(candidate) - 1)
    )


def _has_disabled_cacheprovider(argv: list[str]) -> bool:
    for index, argument in enumerate(argv):
        if argument in {"-pno:cacheprovider", "-p=no:cacheprovider"}:
            return True
        if (
            argument == "-p"
            and index + 1 < len(argv)
            and argv[index + 1] == "no:cacheprovider"
        ):
            return True
    return False


def _capture_diff(checkout_dir: Path, timeout: float) -> str:
    tracked = _run_captured(
        ["git", "-C", str(checkout_dir), "diff", "--binary", "HEAD", "--", "."],
        timeout=timeout,
    )
    _require_zero("branch_push", tracked)
    chunks = [tracked.stdout or ""]

    untracked = _run_captured(
        [
            "git",
            "-C",
            str(checkout_dir),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        timeout=timeout,
    )
    _require_zero("branch_push", untracked)
    for relative_name in (untracked.stdout or "").split("\0"):
        if not relative_name:
            continue
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RepoRunFailed(
                "branch_push",
                f"git reported unsafe untracked path: {relative_name!r}",
            )
        added = _run_captured(
            [
                "git",
                "-C",
                str(checkout_dir),
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative_name,
            ],
            timeout=timeout,
        )
        _require_zero("branch_push", added, allowed_returncodes=(0, 1))
        chunks.append(added.stdout or "")
    return "".join(chunks)


def _locate_agency_source(source: Path) -> tuple[Path | None, str]:
    if source.is_file():
        metadata_path = source.parent / "agency.toml"
        if metadata_path.is_file():
            return source.parent, _read_declared_entry(metadata_path, source.parent)
        return None, source.name
    if not source.is_dir():
        raise ValueError(f"agency source does not exist: {source}")

    metadata_path = source / "agency.toml"
    if metadata_path.is_file():
        return source, _read_declared_entry(metadata_path, source)
    return source, _choose_yaml_entry(source)


def _read_declared_entry(metadata_path: Path, package_root: Path) -> str:
    try:
        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read agency metadata: {exc}") from exc
    agency = metadata.get("agency", {})
    if not isinstance(agency, dict):
        raise ValueError("agency.toml has an invalid [agency] table")
    entry = agency.get("entry", "agent.openai.yaml")
    if not isinstance(entry, str) or not entry:
        raise ValueError("agency.toml entry must be a non-empty string")

    entry_path = Path(entry)
    if (
        entry_path.is_absolute()
        or ".." in entry_path.parts
        or not (package_root / entry_path).is_file()
    ):
        raise ValueError(f"agency.toml entry is unsafe or missing: {entry!r}")
    return entry_path.as_posix()


def _choose_yaml_entry(package_root: Path) -> str:
    candidates = sorted(
        path.relative_to(package_root)
        for path in package_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in {".yaml", ".yml"}
        and not _ignored_agency_path(path.relative_to(package_root))
    )
    preferred_names = ("agent.openai.yaml", "agent.yaml", "parent.yaml", "manager.yaml")
    for preferred in preferred_names:
        preferred_path = Path(preferred)
        if preferred_path in candidates:
            return preferred
    manager_candidates = [
        path
        for path in candidates
        if "manager" in path.stem.lower() or "parent" in path.stem.lower()
    ]
    if len(manager_candidates) == 1:
        return manager_candidates[0].as_posix()
    if len(candidates) == 1:
        return candidates[0].as_posix()
    raise ValueError("agency directory has no unambiguous entry YAML")


def _read_agency_files(package_root: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    readme = ""
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        if (
            not path.is_file()
            or path.is_symlink()
            or _ignored_agency_path(relative)
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"agency package contains a non-text file: {relative}"
            ) from exc
        if relative.as_posix() == "README.md":
            readme = content
        else:
            files[relative.as_posix()] = content
    return files, readme


def _ignored_agency_path(relative: Path) -> bool:
    return (
        any(part in {".git", "__pycache__", ".fabri"} for part in relative.parts)
        or relative.name == ".DS_Store"
        or relative.suffix == ".pyc"
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "agency"


def _resolve_slack_channel(source_config: Path) -> str | None:
    environment_channel = os.environ.get("FABRI_SLACK_CHANNEL")
    if environment_channel and environment_channel.strip():
        return environment_channel.strip()

    _, entry = _locate_agency_source(source_config)
    config_path = (
        source_config / entry
        if source_config.is_dir()
        else (
            source_config.parent / entry
            if (source_config.parent / "agency.toml").is_file()
            else source_config
        )
    )
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RepoRunFailed(
            "notify_slack",
            f"could not read Slack channel from agency config: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise RepoRunFailed("notify_slack", "agency config is not a mapping")

    routing = parsed.get("routing")
    slack = routing.get("slack") if isinstance(routing, dict) else None
    configured = slack.get("default_channel") if isinstance(slack, dict) else None
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return None


def _redact_value(value: object, known_tokens: list[str]) -> object:
    if isinstance(value, str):
        return _redact(value, *known_tokens)
    if isinstance(value, Path):
        return _redact(str(value), *known_tokens)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            _redact(key, *known_tokens): _redact_value(item, known_tokens)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, known_tokens) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value, *known_tokens)


def _redacted_jsonl_lines(text: str, known_tokens: list[str]) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            lines.append(_redact(raw_line, *known_tokens))
            continue
        lines.append(
            json.dumps(
                _redact_value(record, known_tokens),
                sort_keys=True,
            )
        )
    return lines


def _logged_gate_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in text.splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and record.get("type") == "repo_run_gate"
            and isinstance(record.get("gate"), str)
        ):
            names.add(record["gate"])
    return names


def _optional_line(value: object, known_tokens: list[str]) -> str:
    if value is None:
        return ""
    return f"{_redact(value, *known_tokens)}\n"
