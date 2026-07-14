# Agency kernel reference

Use this in installed Claude Code and Codex plugin copies. It mirrors the
public kernel in `docs/agency-kernel.md`.

```text
intake -> inspect -> plan -> delegate -> execute -> verify
                                      ^              |
                                      |--- repair ---|
                                             |
                                      approve -> deliver -> learn
```

The kernel is a fabri operating convention, not a new runtime feature. Intake
defines one deliverable, inputs, a proof bar, and an approval gate. The parent
agent inspects and plans in fabri's ReAct loop, then delegates known roles with
`tools.agents[]`; each child is an ordinary tool with its own agent session,
trace, and budget. Specialists execute built-ins or custom manifest-backed
tools. A custom tool receives JSON on stdin and returns JSON on stdout.

Verification must judge a concrete artifact. Use a deterministic tool or a
trusted `agent.repair.verify_command`; the optional repair loop reruns a failed
agent within configured bounds, but it is a retry mechanism, not an acceptance
gate — after retries are exhausted, fabri returns the parent's last result
regardless of the final verifier outcome. Read the verifier's own `ok`/verdict
output, not CLI success, to know whether the deliverable is actually good.
Deliver the artifact path and verdict. Fabri's normal trace/memory pipeline can
retrieve and promote lessons on later related runs when the agency keeps a
stable SQLite or Qdrant collection.

Static `tools.agents[]` specialists each run as a separate child session with
their own step budget (`agent.max_steps` in the child's own config, not the
parent's `agent.subagent.max_steps`, which only bounds `spawn_subagent`). Child
cost does not currently roll into the parent's reported total either —
`fabri report` on the parent session undercounts the full run.

Per-agency decisions are the persona, deliverable, roles and prompts, tools,
artifact paths, policy, verifier, provider, budgets, and memory collection.
Fabri currently does not provide a first-class agency object, durable workflow
state machine, prompt-Markdown loader in `agent.yaml`, or human-approval UI.

Observe every run, not just the deliverable: `fabri traces list`/`show
<session_id>`/`tail <session_id>`, `fabri report --since 1h` for aggregate
cost/outcome, and `.fabri/logs/<session_id>.log` for DEBUG-level detail. The
parent's own trace only shows which specialists it called and what they
returned, not each specialist's internal steps — show a specialist's own
session ID separately for that. Name every relevant session ID in the
delivery output so a reviewer isn't stuck re-reading the final chat message as
the only evidence a run happened.
