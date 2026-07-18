"""Changelog-to-release-notes agency template."""

from __future__ import annotations

FILES: dict[str, str] = {
    "agent.openai.yaml": '''agent:
  name: changelog-release-notes
  max_steps: 10
  subagent:
    max_steps: 4
  system_prompt: |
    You direct a fixed release-notes agency. The requested deliverable is a
    Markdown file. Always call release_research first, then release_writer,
    then release_verifier. Give every specialist the complete task, including
    source and output paths. Finish only when release_verifier reports a pass;
    otherwise ask release_writer to repair the named failure and verify again.
  repair:
    enabled: true
    max_attempts: 1
    verify_command:
      - python3
      - __AGENCY_ROOT__/scripts/check_release_notes.py
      - __AGENCY_ROOT__/deliverables/release_notes.md
    repair_prompt: |
      A verification check rejected the release notes. Call release_writer
      again with source_path=source/release_input.json
      and output_path=deliverables/release_notes.md,
      fixing ONLY the issues below, then call release_verifier again:

      {errors}

llm:
  provider: openai
  model: gpt-5.6-terra
  max_tokens: 1024
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [release_research, release_writer, release_verifier]
  sandbox_root: __AGENCY_ROOT__
  result_format: toon
  agents:
    - name: release_research
      description: Inspect the release input and return only supported facts and omissions.
      config: __AGENCY_ROOT__/researcher.openai.yaml
    - name: release_writer
      description: Create the requested release-notes Markdown deliverable from the supplied JSON input.
      config: __AGENCY_ROOT__/writer.openai.yaml
    - name: release_verifier
      description: Verify the requested release-notes file and return a concrete pass/fail verdict.
      config: __AGENCY_ROOT__/verifier.openai.yaml

memory:
  backend: sqlite
  collection: changelog_release_notes_parent
  sqlite_path: .fabri/changelog_release_notes.db
  top_k: 3
  record_postmortems: true
''',
    "researcher.openai.yaml": '''agent:
  name: changelog-release-researcher
  max_steps: 4
  system_prompt: |
    You are the research specialist. Read the JSON source path in the task.
    Return a compact factual inventory of release version, date, features,
    fixes, and known limitations. Do not invent facts and do not write files.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [read_file]
  sandbox_root: __AGENCY_ROOT__
  result_format: toon

memory:
  backend: sqlite
  collection: changelog_release_notes_researcher
  sqlite_path: .fabri/changelog_release_notes.db
  top_k: 2
''',
    "writer.openai.yaml": '''agent:
  name: changelog-release-writer
  max_steps: 4
  system_prompt: |
    You are the delivery specialist. Use build_release_notes exactly once with
    the source JSON path and requested output path from the task. The tool is
    authoritative for formatting and writes the deliverable. Report its path
    and do not claim success if the tool returns an error.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir:
    - builtin
    - __AGENCY_ROOT__/tools
  enabled: [build_release_notes]
  sandbox_root: __AGENCY_ROOT__
  result_format: toon

memory:
  backend: sqlite
  collection: changelog_release_notes_writer
  sqlite_path: .fabri/changelog_release_notes.db
  top_k: 2
''',
    "verifier.openai.yaml": '''agent:
  name: changelog-release-verifier
  max_steps: 4
  system_prompt: |
    You are the verification specialist. Use verify_release_notes with the
    source and output paths in the task. Return the tool's boolean verdict and its exact
    missing requirements. Do not approve based on prose alone.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir:
    - builtin
    - __AGENCY_ROOT__/tools
  enabled: [verify_release_notes]
  sandbox_root: __AGENCY_ROOT__
  result_format: toon

memory:
  backend: sqlite
  collection: changelog_release_notes_verifier
  sqlite_path: .fabri/changelog_release_notes.db
  top_k: 2
''',
    "prompts/researcher.md": '''# Research specialist

Read the requested release-input JSON. Return only its supported version, date,
features, fixes, and known limitations. Name omissions; do not invent facts or
write files.
''',
    "prompts/writer.md": '''# Writer specialist

Call `build_release_notes` exactly once with the task's source JSON and output
paths. Treat its file path as the deliverable; report errors rather than
fabricating completion.
''',
    "prompts/verifier.md": '''# Verifier specialist

Call `verify_release_notes` with the requested output path. Return the boolean
verdict and exact missing requirements. Do not approve an unverified file.
''',
    "source/release_input.json": '''{
  "product": "Fabri Notes",
  "version": "0.4.0",
  "release_date": "2026-07-14",
  "features": [
    "Added CSV export for saved reports.",
    "Added a compact activity view for long projects."
  ],
  "fixes": [
    "Fixed duplicate reminders after a project rename."
  ],
  "known_limitations": [
    "CSV export does not include archived reports."
  ]
}
''',
    "deliverables/release_notes.md": '''# Fabri Notes 0.4.0

Released 2026-07-14.

## What’s new

- Added CSV export for saved reports.
- Added a compact activity view for long projects.

## Fixes

- Fixed duplicate reminders after a project rename.

## Known limitations

- CSV export does not include archived reports.
''',
    "scripts/check_release_notes.py": '''import json
import sys
from pathlib import Path


def main() -> int:
    output = Path(sys.argv[1])
    source = Path("__AGENCY_ROOT__/source/release_input.json")
    if not output.is_file():
        print(json.dumps({"ok": False, "error": f"missing deliverable: {output}"}))
        return 1
    data = json.loads(source.read_text())
    text = output.read_text()
    required = [
        f"# {data['product']} {data['version']}",
        f"Released {data['release_date']}.",
        "## What’s new",
        "## Fixes",
        "## Known limitations",
        *data["features"],
        *data["fixes"],
        *data["known_limitations"],
    ]
    missing = [item for item in required if item not in text]
    print(json.dumps({"ok": not missing, "missing": missing}))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
''',
    "tools/build_release_notes.json": '''{
  "name": "build_release_notes",
  "description": "Create verified-format Markdown release notes from a release-input JSON file. Use once with a source path and output path.",
  "command": ["python3", "build_release_notes.py"],
  "input_schema": {
    "type": "object",
    "required": ["source_path", "output_path"],
    "properties": {
      "source_path": {"type": "string"},
      "output_path": {"type": "string"}
    },
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "required": ["output_path", "sections"],
    "properties": {
      "output_path": {"type": "string"},
      "sections": {"type": "integer"}
    }
  },
  "timeout_s": 10
}
''',
    "tools/verify_release_notes.json": '''{
  "name": "verify_release_notes",
  "description": "Check a release-notes Markdown file for required headings and every source item. Return ok false with concrete failures.",
  "command": ["python3", "verify_release_notes.py"],
  "input_schema": {
    "type": "object",
    "required": ["source_path", "output_path"],
    "properties": {
      "source_path": {"type": "string"},
      "output_path": {"type": "string"}
    },
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "required": ["ok", "failures"],
    "properties": {
      "ok": {"type": "boolean"},
      "failures": {"type": "array", "items": {"type": "string"}}
    }
  },
  "timeout_s": 10
}
''',
    "tools/build_release_notes.py": '''import json
import os
import sys
from pathlib import Path


def sandbox_path(value: str) -> Path:
    root = Path(os.environ.get("FABRI_SANDBOX_ROOT", ".")).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes FABRI_SANDBOX_ROOT")
    return path


def section(title: str, items: list[str]) -> list[str]:
    return [f"## {title}", "", *[f"- {item}" for item in items], ""]


def main(source_path: str, output_path: str) -> int:
    source = sandbox_path(source_path)
    output = sandbox_path(output_path)
    data = json.loads(source.read_text())
    required = ("product", "version", "release_date", "features", "fixes", "known_limitations")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"release input missing: {', '.join(missing)}")
    lines = [
        f"# {data['product']} {data['version']}",
        "",
        f"Released {data['release_date']}.",
        "",
    ]
    lines.extend(section("What’s new", data["features"]))
    lines.extend(section("Fixes", data["fixes"]))
    lines.extend(section("Known limitations", data["known_limitations"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\\n".join(lines))
    print(json.dumps({"output_path": str(output.relative_to(Path.cwd())), "sections": 3}))
    return 0


if __name__ == "__main__":
    args = json.loads(sys.stdin.read())
    try:
        raise SystemExit(main(args["source_path"], args["output_path"]))
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__} while building {args['output_path']}"}))
        raise SystemExit(1)
''',
    "tools/verify_release_notes.py": '''import json
import os
import sys
from pathlib import Path


def sandbox_path(value: str) -> Path:
    root = Path(os.environ.get("FABRI_SANDBOX_ROOT", ".")).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes FABRI_SANDBOX_ROOT")
    return path


def check(source_path: str, output_path: str) -> list[str]:
    source = sandbox_path(source_path)
    output = sandbox_path(output_path)
    data = json.loads(source.read_text())
    if not output.is_file():
        return [f"missing deliverable: {output_path}"]
    text = output.read_text()
    failures = []
    for heading in ("## What’s new", "## Fixes", "## Known limitations"):
        if heading not in text:
            failures.append(f"missing heading: {heading}")
    expected = [
        f"# {data['product']} {data['version']}",
        f"Released {data['release_date']}.",
        *data["features"],
        *data["fixes"],
        *data["known_limitations"],
    ]
    for value in expected:
        if value not in text:
            failures.append(f"missing source item: {value}")
    return failures


def main(output_path: str) -> int:
    failures = check(args["source_path"], output_path)
    print(json.dumps({"ok": not failures, "failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    args = json.loads(sys.stdin.read())
    try:
        raise SystemExit(main(args["output_path"]))
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "failures": [f"{type(exc).__name__} while checking {args['output_path']}"],
        }))
        raise SystemExit(1)
''',
}

# The reference agency includes equivalent Anthropic configs. Derive them from
# the OpenAI copies so the two variants cannot drift apart structurally.
FILES["agent.yaml"] = (
    FILES["agent.openai.yaml"]
    .replace("provider: openai\n  model: gpt-5.6-terra", "provider: anthropic\n  model: claude-haiku-4-5")
    .replace("api_key_env: OPENAI_API_KEY", "api_key_env: ANTHROPIC_API_KEY")
    .replace(".openai.yaml", ".yaml")
)
for role in ("researcher", "writer", "verifier"):
    FILES[f"{role}.yaml"] = (
        FILES[f"{role}.openai.yaml"]
        .replace("provider: openai\n  model: gpt-5.6-luna", "provider: anthropic\n  model: claude-haiku-4-5")
        .replace("api_key_env: OPENAI_API_KEY", "api_key_env: ANTHROPIC_API_KEY")
    )

README = '''# Changelog-to-release-notes agency

This is the agency-builder walking skeleton. It turns one structured changelog
into one Markdown release-notes file. The fixed team is deliberately small:

```text
orchestrator -> release_research -> release_writer -> release_verifier
                                      |                    |
                                      +---- repair <-------+
```

The three specialist configs are static agents-as-tools. The writer calls a
deterministic JSON-manifest tool to materialize the deliverable; the verifier
calls another manifest tool, and `scripts/check_release_notes.py` checks the
same headings and source items as a trusted repair command. Treat these as two
independent checks of the same proof bar.

The committed `deliverables/release_notes.md` was produced from the included
source and passes both deterministic checks. It is not evidence of a completed
multi-agent live run; that requires an API key and network access.

## Run

Run fabri from `__RUN_FROM__`, because paths in the generated configs are
relative to that directory:

```bash
export OPENAI_API_KEY=...
fabri serve --config __AGENCY_ROOT__/agent.openai.yaml
fabri studio
```

Ask it to create verified release notes from `source/release_input.json` at
`deliverables/release_notes.md`.

## Test the real tool contract without a model

The trusted check can be run directly from the same directory:

```bash
python3 __AGENCY_ROOT__/scripts/check_release_notes.py \\
  __AGENCY_ROOT__/deliverables/release_notes.md
```

The prompts under `prompts/` are review copies of each config's
`agent.system_prompt`. Fabri's current YAML config accepts the prompt string;
it does not load a prompt Markdown path automatically.

## After a live run

`fabri run` prints a session ID. Use `fabri traces show <session_id>` for the
parent trace and `fabri report --since 1h` for recent aggregate cost/outcome.
Each static specialist has its own child session ID, visible in the parent
trace's tool-call result.
'''

TEMPLATE = {"FILES": FILES, "README": README}
