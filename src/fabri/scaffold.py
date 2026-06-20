"""`fabri init` scaffolding: write a minimal, runnable starter project so a new
user goes from `pip install fabri` to a working agent in one command. Templates
are inline (no package-data needed)."""
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

# relative path -> file contents
_FILES = {
    "agent.yaml": _AGENT_YAML,
    "tools/agent_tools/hello.json": _HELLO_JSON,
    "tools/agent_tools/hello.py": _HELLO_PY,
    "docker-compose.yml": _DOCKER_COMPOSE,
    ".gitignore": _GITIGNORE,
}


def scaffold(target_dir: str, force: bool = False) -> dict:
    """Write the starter project into `target_dir`. Existing files are left
    untouched unless `force`. Returns {"created": [...], "skipped": [...]}."""
    root = Path(target_dir)
    created, skipped = [], []
    for rel, content in _FILES.items():
        path = root / rel
        if path.exists() and not force:
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(rel)
    return {"created": created, "skipped": skipped}


def next_steps(target_dir: str) -> str:
    where = "" if target_dir in (".", "") else f"cd {target_dir}\n  "
    return (
        "Next:\n"
        f"  {where}docker compose up -d                 # start Qdrant on :6333\n"
        "  export ANTHROPIC_API_KEY=...          # your Anthropic API key\n"
        '  fabri --config agent.yaml run "greet Ada with the hello tool"'
    )
