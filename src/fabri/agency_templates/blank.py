"""Minimal manager-and-specialist agency template."""

from __future__ import annotations

FILES: dict[str, str] = {
    "agent.openai.yaml": '''agent:
  name: blank-agency
  max_steps: 8
  system_prompt: |
    You manage one specialist. Delegate the requested workspace task to
    specialist, inspect its result, and return a concise final answer.

llm:
  provider: openai
  model: gpt-5.6-terra
  max_tokens: 1024
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [specialist]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon
  agents:
    - name: specialist
      description: Inspect the workspace and complete the delegated task.
      config: __AGENCY_ROOT__/specialist.openai.yaml

memory:
  backend: sqlite
  collection: blank_agency_parent
  sqlite_path: .fabri/blank_agency.db
  top_k: 3
''',
    "specialist.openai.yaml": '''agent:
  name: blank-specialist
  max_steps: 5
  system_prompt: |
    You are the agency specialist. Inspect files before changing them, make
    only changes required by the task, and report what you verified.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [read_file, list_dir, edit_file, bash]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon

memory:
  backend: sqlite
  collection: blank_agency_specialist
  sqlite_path: .fabri/blank_agency.db
  top_k: 2
''',
    "workspace/.gitignore": "__pycache__/\n*.pyc\n",
    "workspace/store.py": '''"""Replace this starter module with your agency's workspace code."""


def ready() -> bool:
    return True
''',
    "workspace/test_store.py": '''"""Self-locating smoke test for the blank workspace."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import ready


def test_ready():
    assert ready()
''',
}

README = '''# Blank fabri agency

A minimal manager plus one editable specialist and workspace.

Run fabri from `__RUN_FROM__`, because paths in the generated configs are
relative to that directory:

```bash
export OPENAI_API_KEY=...
fabri serve --config __AGENCY_ROOT__/agent.openai.yaml
fabri studio
```
'''

TEMPLATE = {"FILES": FILES, "README": README}
