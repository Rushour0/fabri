"""`fabri init` scaffolding: write a minimal, runnable starter project so a new
user goes from `pip install fabri` to a working agent in one command.

G18 (templates): `fabri init --template <name>` picks a vetted starter pack for
a common task shape (research / code-review / data-cleanup), each pre-wired
with a relevant config + 1-2 example tools. The default (no --template) is the
generic hello-tool starter we always shipped.

Templates are inline (no package-data needed)."""
from pathlib import Path

_AGENT_YAML = """\
agent:
  name: my-agent
  max_steps: 10

llm:
  provider: anthropic           # or "openai" (pip install "fabri[openai]")
  model: claude-sonnet-4-6
  max_tokens: 1024
  api_key_env: ANTHROPIC_API_KEY

tools:
  manifest_dir:
    - builtin                   # fabri's bundled tools (read_file, write_file, list_dir, ...)
    - tools/agent_tools         # your own tools, relative to where you run fabri
  enabled: [read_file, write_file, list_dir, hello]
  sandbox_root: .               # file tools are jailed to this directory
  result_format: toon           # compact tool results -> fewer input tokens

memory:
  backend: qdrant               # or "sqlite" (no docker; pip install 'fabri[sqlite]')
  collection: my_agent          # this agent's own Qdrant collection
  qdrant_url: http://localhost:6333
"""

_HELLO_JSON = """\
{
  "name": "hello",
  "description": "Returns a friendly greeting for the given name. Example of a custom tool.",
  "command": ["python3", "hello.py"],
  "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
  "output_schema": {"type": "object"},
  "timeout_s": 10
}
"""

_HELLO_PY = '''\
"""A minimal fabri tool: read one JSON object from stdin, print one to stdout."""
import json
import sys


def main() -> int:
    args = json.loads(sys.stdin.read())
    print(json.dumps({"greeting": f"Hello, {args['name']}! \\u2014 from your fabri tool."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_DOCKER_COMPOSE = """\
services:
  qdrant:
    image: qdrant/qdrant
    ports:
      - "6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage
volumes:
  qdrant_data:
"""

_GITIGNORE = ".fabri/\n"


# ---------------------------------------------------------------------------
# G18 — starter agent templates: vetted task-shaped configs + 1-2 tools.
# Each template is a self-contained {relative_path: content} dict.
# ---------------------------------------------------------------------------

# --- research template -------------------------------------------------------

_RESEARCH_YAML = """\
agent:
  name: research-agent
  max_steps: 25
  # Research tasks benefit from decomposing the question into sub-queries
  # the agent can answer separately, then synthesize.
  planner:
    enabled: true
    mode: auto

llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_tokens: 2048
  api_key_env: ANTHROPIC_API_KEY

tools:
  manifest_dir:
    - builtin
    - tools/agent_tools
  enabled: [read_file, write_file, list_dir, web_search, fetch_url]
  sandbox_root: .
  result_format: toon
  decompose:
    enabled: true
    max_subquestions: 5

memory:
  backend: sqlite
  collection: research
  sqlite_path: .fabri/research.db
"""

_FETCH_URL_JSON = """\
{
  "name": "fetch_url",
  "description": "Fetch a URL and return its text content (HTML stripped, max 8KB).",
  "command": ["python3", "fetch_url.py"],
  "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
  "output_schema": {"type": "object"},
  "timeout_s": 30
}
"""

_FETCH_URL_PY = '''\
"""A minimal HTTP fetcher tool. Uses urllib + a tiny HTML stripper."""
import json
import re
import sys
import urllib.request


def strip_html(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\\s+", " ", html).strip()


def main() -> int:
    args = json.loads(sys.stdin.read())
    url = args["url"]
    req = urllib.request.Request(url, headers={"User-Agent": "fabri-research/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read(64 * 1024).decode("utf-8", errors="replace")
    text = strip_html(body)[:8000]
    print(json.dumps({"url": url, "text": text}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# --- code-review template ----------------------------------------------------

_CODE_REVIEW_YAML = """\
agent:
  name: code-review-agent
  max_steps: 20
  system_prompt_prefix: |
    You are a senior code reviewer. For each change, evaluate:
    correctness, readability, architecture, security, and performance.
    Cite file:line for every finding. Prefer concrete patches over vague advice.

llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_tokens: 2048
  api_key_env: ANTHROPIC_API_KEY

tools:
  manifest_dir:
    - builtin
    - tools/agent_tools
  enabled: [read_file, list_dir, run_shell]
  sandbox_root: .
  result_format: toon

memory:
  backend: sqlite
  collection: code-review
  sqlite_path: .fabri/code-review.db
"""

_RUN_SHELL_JSON = """\
{
  "name": "run_shell",
  "description": "Run a single shell command in the project root and return its stdout/stderr/exit code. Read-only commands only (git diff, ls, cat) — refuse destructive ones.",
  "command": ["python3", "run_shell.py"],
  "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
  "output_schema": {"type": "object"},
  "timeout_s": 30
}
"""

_RUN_SHELL_PY = '''\
"""A whitelisted shell runner. Refuses anything that isn't read-only-ish."""
import json
import shlex
import subprocess
import sys

ALLOWED_BINS = {"git", "ls", "cat", "head", "tail", "grep", "find", "wc", "diff", "pwd", "echo"}
DENY_TOKENS = {"rm", "mv", "cp", ">", ">>", "|", "&&", "||", ";", "`", "$("}


def main() -> int:
    args = json.loads(sys.stdin.read())
    cmd = args["cmd"]
    if any(tok in cmd for tok in DENY_TOKENS):
        print(json.dumps({"error": "refused: contains a deny-token (|, >, ;, rm, ...)"}))
        return 1
    parts = shlex.split(cmd)
    if not parts or parts[0] not in ALLOWED_BINS:
        print(json.dumps({"error": f"refused: binary not in allow-list. allowed={sorted(ALLOWED_BINS)}"}))
        return 1
    try:
        proc = subprocess.run(parts, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "timeout"}))
        return 1
    print(json.dumps({"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-1000:], "exit": proc.returncode}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# --- data-cleanup template ---------------------------------------------------

_DATA_CLEANUP_YAML = """\
agent:
  name: data-cleanup-agent
  max_steps: 30

llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_tokens: 2048
  api_key_env: ANTHROPIC_API_KEY

tools:
  manifest_dir:
    - builtin
    - tools/agent_tools
  enabled: [read_file, write_file, list_dir, python_exec]
  sandbox_root: data
  result_format: toon

memory:
  backend: sqlite
  collection: data-cleanup
  sqlite_path: .fabri/data-cleanup.db
"""


# ---------------------------------------------------------------------------
# Templates registry
# ---------------------------------------------------------------------------

_DEFAULT_FILES = {
    "agent.yaml": _AGENT_YAML,
    "tools/agent_tools/hello.json": _HELLO_JSON,
    "tools/agent_tools/hello.py": _HELLO_PY,
    "docker-compose.yml": _DOCKER_COMPOSE,
    ".gitignore": _GITIGNORE,
}

_RESEARCH_FILES = {
    "agent.yaml": _RESEARCH_YAML,
    "tools/agent_tools/fetch_url.json": _FETCH_URL_JSON,
    "tools/agent_tools/fetch_url.py": _FETCH_URL_PY,
    ".gitignore": _GITIGNORE,
}

_CODE_REVIEW_FILES = {
    "agent.yaml": _CODE_REVIEW_YAML,
    "tools/agent_tools/run_shell.json": _RUN_SHELL_JSON,
    "tools/agent_tools/run_shell.py": _RUN_SHELL_PY,
    ".gitignore": _GITIGNORE,
}

_DATA_CLEANUP_FILES = {
    "agent.yaml": _DATA_CLEANUP_YAML,
    "data/.gitkeep": "",
    ".gitignore": _GITIGNORE,
}

SCAFFOLD_TEMPLATES = {
    "default": _DEFAULT_FILES,
    "research": _RESEARCH_FILES,
    "code-review": _CODE_REVIEW_FILES,
    "data-cleanup": _DATA_CLEANUP_FILES,
}


def scaffold(target_dir: str, force: bool = False, template: str = "default") -> dict:
    """Write the starter project into `target_dir`. Existing files are left
    untouched unless `force`. Returns {"created": [...], "skipped": [...]}.

    `template`: one of SCAFFOLD_TEMPLATES.keys(). Default ('default') is the
    generic hello-tool scaffold. Other templates ship config + tools for a
    common task shape (research, code-review, data-cleanup)."""
    files = SCAFFOLD_TEMPLATES.get(template)
    if files is None:
        raise ValueError(
            f"unknown template {template!r}; pick one of: {sorted(SCAFFOLD_TEMPLATES)}"
        )
    root = Path(target_dir)
    created, skipped = [], []
    for rel, content in files.items():
        path = root / rel
        if path.exists() and not force:
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(rel)
    return {"created": created, "skipped": skipped, "template": template}


def next_steps(target_dir: str, template: str = "default") -> str:
    where = "" if target_dir in (".", "") else f"cd {target_dir}\n  "
    # The non-default templates use sqlite-vec, so docker isn't required.
    if template == "default":
        return (
            "Next:\n"
            f"  {where}docker compose up -d                 # start Qdrant on :6333\n"
            "  export ANTHROPIC_API_KEY=...          # your Anthropic API key\n"
            '  fabri --config agent.yaml run "greet Ada with the hello tool"'
        )
    sample_tasks = {
        "research": 'fabri --config agent.yaml run "Summarize the README at https://example.com"',
        "code-review": 'fabri --config agent.yaml run "Review the changes in git diff HEAD~1"',
        "data-cleanup": 'fabri --config agent.yaml run "Read data/input.csv and emit cleaned data/output.csv"',
    }
    sample = sample_tasks.get(template, 'fabri --config agent.yaml run "your task here"')
    return (
        f"Next ({template} template, sqlite-vec backend — no docker needed):\n"
        f"  {where}pip install 'fabri[sqlite]'         # embedded vector store\n"
        "  export ANTHROPIC_API_KEY=...          # your Anthropic API key\n"
        f"  {sample}"
    )
