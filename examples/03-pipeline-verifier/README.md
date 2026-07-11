# 03 · Pipeline with a verifier (static agents-as-tools)

**What you'll learn:** the *static* way to compose agents — declare specialist
sub-agents once in `tools.agents[]`, and the orchestrator calls each one as an
ordinary tool with a uniform contract.

**Optimization methodology demonstrated:** *adversarial verification behind a
uniform contract* — a skeptical second agent guards the first agent's
confident-but-wrong output, the same "check before you commit" move both
surveyed projects use (OpenClaw's human-review promotion gate, Hermes'
extract-then-verify). See `docs/optimization-methodologies.md`.

## Static vs. dynamic spawning

This is the counterpart to example 02. There the parent chose children at
runtime with `spawn_subagent(config_path=...)`. Here the roles are known up
front, so they're declared once:

```yaml
tools:
  enabled: [read_file, draft, verify]   # the agent names are tools too
  agents:
    - name: draft
      description: Produce a candidate answer. May be wrong.
      config: examples/03-pipeline-verifier/drafter.yaml
    - name: verify
      description: Check a candidate against its source. Returns {ok, reasons[]}.
      config: examples/03-pipeline-verifier/verifier.yaml
```

Each entry becomes a tool named `draft` / `verify`. Calling it spawns a fresh
agent loop running that config in a subprocess, returning
`{final_text, outcome, session_id, trace_path, usage}` — the same envelope
`spawn_subagent` returns. The **description is the contract**: it's all the
orchestrator sees when deciding to call it.

## The files

```
03-pipeline-verifier/
├── parent.yaml     # orchestrator: runs draft → verify → revise
├── drafter.yaml    # produces a candidate (cheap flash model, read-only)
└── verifier.yaml   # the skeptic: returns {ok, reasons[]}
```

## Run it

```bash
pip install 'fabri[sqlite]'
export GEMINI_API_KEY=...
fabri --config examples/03-pipeline-verifier/parent.yaml run \
  "Write a one-paragraph explanation of what fabri's memory store is, grounded \
   ONLY in docs/using-fabri-well.md. Draft it, then verify it against that \
   file, and revise until verify returns ok."
```

## The loop the orchestrator runs

```text
draft(task, source)              → candidate
verify(candidate, source)        → {ok: false, reasons: ["claim X unsupported", ...]}
draft(task, source, reasons)     → revised candidate      (feed the reasons back)
verify(revised, source)          → {ok: true}
→ return the revised answer
```

Two cheap, single-purpose models beat one expensive model asked to
self-check: the verifier has no stake in the draft being right, so it catches
what the drafter rationalizes. Give each a tight `agent.subagent.max_steps`
budget (set in `parent.yaml`) so the loop can't run away.

## When to use static vs. dynamic

- **Static (`tools.agents[]`)** — fixed pipeline of known specialist roles
  (classify → route → draft → verify). Roles are visible in the config and to
  the model as named tools.
- **Dynamic (`spawn_subagent`)** — the number and shape of subtasks depends on
  the input (example 02's fan-out). One generic worker config, invoked N times.

You can mix both in one agent.

## Next

- `../04-docker-sandbox/` — run any of these inside a locked-down container.
