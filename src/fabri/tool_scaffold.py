"""G14: `fabri tool init <lang> <name>` — scaffold a new tool with the
right manifest + executable stub in the picked language.

Languages supported: python, go, node, bash. Adding one is ~30 lines of
template + register-in-_TEMPLATES below.

All templates produce a tool that conforms to fabri's contract:
- stdin: one JSON object of args
- stdout: one JSON object of result (or `{"error": "..."}` on failure)
- exit code: 0 = ok, non-zero = error (the runner wraps either way)

Each template ships with a minimal "echo" implementation as a starting point —
the user replaces the body. The manifest comes pre-wired with input/output
schemas marked as opaque objects (the agent's LLM is happy to send/receive
JSON; users tighten schemas as the tool stabilizes).
"""
from __future__ import annotations

import json
from pathlib import Path


def _manifest(name: str, command: list[str], description: str | None = None) -> str:
    return json.dumps(
        {
            "name": name,
            "description": description
            or f"Tool {name} — describe what this does in one sentence.",
            "command": command,
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "timeout_s": 10.0,
        },
        indent=2,
    ) + "\n"


_PY_STUB = '''\
"""{name} — fabri tool scaffolded by `fabri tool init python {name}`."""
import json
import sys


def main() -> int:
    args = json.loads(sys.stdin.read())
    # TODO: replace with your tool's logic. The agent sends `args`; you return
    # any JSON-serialisable dict on stdout.
    result = {{"echo": args}}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_GO_STUB = '''\
// {name} — fabri tool scaffolded by `fabri tool init go {name}`.
package main

import (
\t"encoding/json"
\t"fmt"
\t"io"
\t"os"
)

func main() {{
\tdata, err := io.ReadAll(os.Stdin)
\tif err != nil {{
\t\tfmt.Println(`{{"error":"read stdin"}}`)
\t\tos.Exit(1)
\t}}
\tvar args map[string]any
\t_ = json.Unmarshal(data, &args)
\tout, _ := json.Marshal(map[string]any{{"echo": args}})
\tfmt.Println(string(out))
}}
'''

_NODE_STUB = '''\
// {name} — fabri tool scaffolded by `fabri tool init node {name}`.
let buf = "";
process.stdin.on("data", (c) => {{ buf += c; }});
process.stdin.on("end", () => {{
  try {{
    const args = JSON.parse(buf);
    // TODO: your tool's logic here.
    process.stdout.write(JSON.stringify({{ echo: args }}));
  }} catch (e) {{
    process.stdout.write(JSON.stringify({{ error: String(e) }}));
    process.exit(1);
  }}
}});
'''

_BASH_STUB = '''\
#!/usr/bin/env bash
# {name} — fabri tool scaffolded by `fabri tool init bash {name}`.
# Replace with your logic; remember: stdin = one JSON line, stdout = one JSON.
input=$(cat)
echo "{{\\"echo\\":${{input}}}}"
'''


# (language, file_suffix, executable_relpath, command_template, stub_template)
_TEMPLATES: dict[str, dict] = {
    "python": {
        "ext": "py",
        "command": lambda exe: ["python3", exe],
        "stub": _PY_STUB,
        "chmod": False,
    },
    "go": {
        "ext": "go",
        "command": lambda exe: ["go", "run", exe],
        "stub": _GO_STUB,
        "chmod": False,
    },
    "node": {
        "ext": "js",
        "command": lambda exe: ["node", exe],
        "stub": _NODE_STUB,
        "chmod": False,
    },
    "bash": {
        "ext": "sh",
        "command": lambda exe: ["bash", exe],
        "stub": _BASH_STUB,
        "chmod": True,
    },
}


SUPPORTED_LANGUAGES = sorted(_TEMPLATES.keys())


def scaffold_tool(lang: str, name: str, target_dir: Path, force: bool = False) -> dict:
    """Scaffold a tool: writes <target_dir>/<name>.json + <target_dir>/<name>.<ext>.

    Returns {"created": [...], "skipped": [...]}.
    """
    if lang not in _TEMPLATES:
        raise ValueError(
            f"unknown language {lang!r}; pick one of: {SUPPORTED_LANGUAGES}"
        )
    if not name.replace("_", "").isalnum():
        raise ValueError(
            f"tool name must be alphanumeric+underscore (got {name!r})"
        )
    tmpl = _TEMPLATES[lang]
    exe_name = f"{name}.{tmpl['ext']}"
    manifest_path = target_dir / f"{name}.json"
    exe_path = target_dir / exe_name

    target_dir.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []

    for path, content in (
        (manifest_path, _manifest(name, tmpl["command"](exe_name))),
        (exe_path, tmpl["stub"].format(name=name)),
    ):
        if path.exists() and not force:
            skipped.append(str(path.name))
            continue
        path.write_text(content)
        if tmpl.get("chmod"):
            path.chmod(0o755)
        created.append(str(path.name))

    return {"created": created, "skipped": skipped, "language": lang, "name": name}
