"""Run the code-review crew and publish its single GitHub PR summary comment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping


def build_task(diff: str, pr_title: str, pr_number: int) -> str:
    """Build the self-contained task supplied to the agency."""
    return (
        f"Review pull request #{pr_number}: {pr_title}\n\n"
        "The unified diff is also available at workspace/pr.diff. Review only "
        "changes substantiated by this diff and return the JSON verdict.\n\n"
        f"```diff\n{diff}\n```"
    )


def parse_run_output(stdout: str) -> dict[str, object]:
    """Extract validated structured output from fabri's JSON result output."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            result, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(result, Mapping):
            continue
        if "structured_output" not in result:
            continue
        structured_output = result["structured_output"]
        if not isinstance(structured_output, dict):
            raise ValueError("fabri result structured_output must be a JSON object")
        return structured_output
    raise ValueError("fabri run output did not contain a result with structured_output")


def format_comment(verdict: dict[str, object]) -> str:
    """Render a readable single-comment summary from a validated verdict."""
    verdict_name = str(verdict.get("verdict", "comment")).replace("_", " ").title()
    summary = str(verdict.get("summary", "No summary was provided."))
    findings_value = verdict.get("findings", [])
    findings = findings_value if isinstance(findings_value, list) else []
    lines = [f"## Fabri code review — {verdict_name}", "", summary, ""]
    if not findings:
        lines.extend(["No findings. Nice work!", ""])
        return "\n".join(lines)

    for severity in ("blocker", "warning", "nit"):
        group = [item for item in findings if isinstance(item, Mapping) and item.get("severity") == severity]
        if not group:
            continue
        lines.extend([f"### {severity.title()} findings", "", "| File | Line | Finding |", "| --- | ---: | --- |"])
        for finding in group:
            file_name = str(finding.get("file", "Unknown"))
            line = finding.get("line")
            line_text = "—" if line is None else str(line)
            note = str(finding.get("note", "")).replace("\n", " ")
            lines.append(f"| `{file_name}` | {line_text} | {note} |")
        lines.append("")
    return "\n".join(lines)


def exit_code_for(verdict: dict[str, object], fail_on: str) -> int:
    """Return a failing process code when the verdict meets the failure threshold."""
    levels = {"approve": 0, "comment": 1, "request_changes": 2}
    verdict_level = levels.get(str(verdict.get("verdict", "approve")), 0)
    fail_level = levels.get(fail_on, 2)
    return int(verdict_level >= fail_level)


def main() -> int:
    config = os.environ["FABRI_CONFIG"]
    pr_number = int(os.environ["PR_NUMBER"])
    repository = os.environ["GITHUB_REPOSITORY"]
    pr_title = os.environ.get("PR_TITLE", f"Pull request #{pr_number}")
    diff_path = "examples/agencies/code-review-crew/workspace/pr.diff"
    with open(diff_path, encoding="utf-8") as diff_file:
        task = build_task(diff_file.read(), pr_title, pr_number)

    completed = subprocess.run(
        ["fabri", "--config", config, "run", task],
        check=True,
        capture_output=True,
        text=True,
    )
    verdict = parse_run_output(completed.stdout)
    comment = format_comment(verdict)
    subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--repo", repository, "--body-file", "-"],
        check=True,
        input=comment,
        text=True,
    )
    return exit_code_for(verdict, os.environ.get("FAIL_ON", "request_changes"))


if __name__ == "__main__":
    sys.exit(main())
