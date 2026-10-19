"""Jev-routed agency template: a decision tree picks the specialist."""

from __future__ import annotations

import textwrap

from fabri.style import PLAIN_WRITING_RULES

# Every specialist answers a person directly, so each one carries the same
# plain-writing rules as a prompt prefix.
_RULES_BLOCK = textwrap.indent(PLAIN_WRITING_RULES, "    ")


def _specialist(name: str, role: str, tools: str, max_steps: int = 5) -> str:
    return f'''agent:
  name: __AGENCY_SLUG__-{name}
  max_steps: {max_steps}
  system_prompt_prefix: |
{_RULES_BLOCK}
  system_prompt: |
{role}
llm:
  provider: openai
  model: gpt-5-mini
  # gpt-5-mini spends part of this budget on hidden reasoning tokens.
  max_tokens: 2048
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [{tools}]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon

memory:
  backend: sqlite
  collection: __AGENCY_SLUG___{name}
  sqlite_path: .fabri/__AGENCY_SLUG__.db
  top_k: 2
'''


FILES: dict[str, str] = {
    "tree.yaml": '''# Routes one request to a specialist. Jev answers first; any answer below
# its threshold is re-asked on the LLM fallback. Act or no-act outcomes end
# at leaves with requires_approval: true, because Jev is weak at deciding
# whether to act at all. Your host must hold those for a human.
name: __AGENCY_SLUG__-router

decider:
  kind: cascade
  primary:
    kind: jev
    provider: openrouter
    model: typesafe/jev-1.13
  fallback:
    kind: llm
    llm:
      provider: openai
      model: gpt-5-mini
      # Reasoning tokens count against this budget; too small returns no answer.
      max_tokens: 2048
      api_key_env: OPENAI_API_KEY
  thresholds:
    choice: 0.6
    score: 0.6
    yesno: 0.8

start: intent

# Non-acting options come first: Jev leans toward the first option it sees.
# Approval is three narrow yes/no checks in a row. Any "yes" ends at an
# approval leaf; only three confident "no" answers skip approval. With the
# yesno threshold at 0.8, an answer is accepted only at P >= 0.9, so an
# unsure check goes to the LLM instead of quietly skipping approval.
nodes:
  intent:
    question:
      kind: choice
      instructions: Which specialist should handle the user's latest request?
      options:
        chat: Questions, explanations and small talk. Changes nothing.
        canvas: Change the content or layout of the shared canvas document. Not for tasks.
        tasks: Create, update, assign, complete or delete a task. Not for canvas text.
    branches:
      chat:
        leaf:
          agent: chat
          config: __AGENCY_ROOT__/chat.openai.yaml
      canvas:
        leaf:
          agent: canvas
          config: __AGENCY_ROOT__/canvas.openai.yaml
      tasks: affects_others

  affects_others:
    question:
      kind: yesno
      instructions: Would doing this notify, assign work to, or change something owned by a person other than the requester?
    branches:
      "true": &needs_approval
        leaf:
          agent: tasks
          config: __AGENCY_ROOT__/tasks.openai.yaml
          requires_approval: true
      "false": hard_to_undo

  hard_to_undo:
    question:
      kind: yesno
      instructions: Would doing this be hard to undo, such as deleting tasks or sending a message?
    branches:
      "true": *needs_approval
      "false": bulk_change

  bulk_change:
    question:
      kind: yesno
      instructions: Does the request change more than three tasks at once?
    branches:
      "true": *needs_approval
      "false":
        leaf:
          agent: tasks
          config: __AGENCY_ROOT__/tasks.openai.yaml
''',
    "chat.openai.yaml": _specialist(
        "chat",
        "    You answer questions about the workspace. Read files before you\n"
        "    answer, do not change anything, and say what you checked.\n",
        "read_file, list_dir",
        max_steps=4,
    ),
    "canvas.openai.yaml": _specialist(
        "canvas",
        "    You edit the canvas documents in the workspace. Read a file before\n"
        "    changing it, make only the requested edit, and report what changed.\n",
        "read_file, list_dir, edit_file",
    ),
    "tasks.openai.yaml": _specialist(
        "tasks",
        "    You manage the task list in the workspace. Make only the requested\n"
        "    change and report the tasks you created or changed.\n",
        "read_file, list_dir, edit_file",
    ),
    "workspace/.gitignore": "__pycache__/\n*.pyc\n",
}

README = '''# Jev router agency

A decision tree in `tree.yaml` picks which specialist handles a request:
`chat`, `canvas`, or `tasks`. The tree runs on a cascade decider.

- **Jev routes among clear options.** It is fast and costs about $0.04 per
  million input tokens, and it does well at picking one option from a list.
- **The LLM fallback takes over below the threshold.** When Jev's confidence
  on a question is under the threshold in `tree.yaml` (0.6 for choices, 0.8
  for yes/no, which means P >= 0.9 for the chosen side since yes/no
  confidence is |2P - 1|), or Jev fails, the question is re-asked on
  `gpt-5-mini`. The
  output marks those answers as escalated.
- **Act or no-act decisions end at approval leaves.** Jev is weak at deciding
  whether to act at all, so the risky-task branch ends at a leaf with
  `requires_approval: true`. fabri passes that flag through untouched; your
  host must hold the run for a human before starting the `tasks` agent.

Run fabri from `__RUN_FROM__`, because paths in the generated configs are
relative to that directory:

```bash
export OPENROUTER_API_KEY=...   # Jev through OpenRouter
export OPENAI_API_KEY=...       # fallback and specialists
fabri decide --tree __AGENCY_ROOT__/tree.yaml --state "Add a task to review the Q3 deck"
```

The leaf names the specialist config to run, for example:

```bash
fabri serve --config __AGENCY_ROOT__/tasks.openai.yaml
fabri studio
```

To call TypeSafe directly instead of OpenRouter, set `provider: typesafe` and
`model: jev-1.13.0` under `decider.primary`, and export `TYPESAFE_API_KEY`.
'''

TEMPLATE = {"FILES": FILES, "README": README, "ENTRY": "chat.openai.yaml"}
