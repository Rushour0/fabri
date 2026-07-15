# Apply the agency frame

Create this layout beneath the target project. Paths in a fabri config resolve
from the directory where `fabri` runs, so show the run command from project
root and use project-root-relative paths.

```text
<agency>/
  agent.yaml
  <role>.yaml
  prompts/<role>.md
  tools/<tool>.json
  tools/<tool>.py
  scripts/check_<deliverable>.py
  source/
  deliverables/
```

## Config mapping

- Put the orchestrator's fixed procedure in `agent.system_prompt`; fabri does
  not currently have a config key that loads a prompt Markdown file. Keep an
  identical review copy under `prompts/` and state that relationship in its
  README.
- Use `tools.manifest_dir: [builtin, <agency>/tools]` and list only required
  names in `tools.enabled`. A manifest has `name`, `description`, `command`,
  `input_schema`, `output_schema`, and `timeout_s`.
- Declare known specialists under `tools.agents[]`. Each needs a stable tool
  name, a concise parent-visible description, and its config path. Add those
  names to the parent's `enabled` list.
- Give specialist configs their own `agent.name`, small `max_steps`, focused
  `system_prompt`, minimal tools, and SQLite memory collection. `tools.agents[]`
  runs a fresh child loop; it does not share the parent's conversation.
- Use `agent.repair.verify_command` only for a trusted local command that exits
  non-zero or prints `{"ok": false}` on failure. The verifier is host code, not
  sandboxed; constrain it to the agency's deliverable path.
- Set `agent.max_cost_usd` to the frame's cost ceiling. Fabri ends a run at
  `Outcome.BUDGET_EXCEEDED` once crossed; leaving it unset is a conscious "no
  ceiling" decision, not a default. Cost + `cost_by_model` + run metrics ride on
  the `usage` event automatically — no extra config.

## Front-end: point Fabri Studio at the agency

Reuse `examples/studio/` rather than building a per-agency UI:

```text
# single-deliverable / conversational agency
fabri --config <agency>/agent.yaml serve            # terminal 1
cd examples/studio && npm install && npm run dev     # terminal 2  → http://localhost:5173
```

Studio streams the run's plan timeline, tool calls, `ask_user` prompts, and a
live COGS panel (per-model cost, budget vs spent). For a **fan-out** agency, use
Studio's Fleet view: it `POST /fleets` fans the batch to N runs and shows the
summed fleet COGS + per-item drill-down. Only build a bespoke dashboard if
Studio genuinely cannot express the needed view.

## Required gates

1. Validate every manifest: `fabri tool validate <manifest>`.
2. Invoke each executable through the runner: `FABRI_SANDBOX_ROOT="$PWD" fabri
   tool test <name> --args '<json>' --dir <tools-dir>`.
3. Inspect the assembled agent without credentials:
   `fabri --config <agency>/agent.yaml run --dry-run "<task>"`.
4. Run the agency with the matching key. Preserve the resulting trace and the
   deliverable path. If no credential is available, report that exact preflight
   failure rather than replacing it with a fake run.
5. Report COGS as part of delivery: quote per-run total cost, `cost_by_model`,
   `outcome`, tool-failure rate, and spent-vs-`max_cost_usd` from `fabri report
   --since 1h` (or Studio's fleet roll-up for a fan-out). An agency whose cost
   you cannot state is not shippable — see SKILL.md's delivery gate.
