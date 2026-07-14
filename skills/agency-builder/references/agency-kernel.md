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
agent within configured bounds. Deliver the artifact path and verdict. Fabri's
normal trace/memory pipeline can retrieve and promote lessons on later related
runs when the agency keeps a stable SQLite or Qdrant collection.

Per-agency decisions are the persona, deliverable, roles and prompts, tools,
artifact paths, policy, verifier, provider, budgets, and memory collection.
Fabri currently does not provide a first-class agency object, durable workflow
state machine, prompt-Markdown loader in `agent.yaml`, or human-approval UI.

Observe every run, not just the deliverable: `fabri traces list`/`show
<session_id>`/`tail <session_id>`, `fabri report --since 1h` for aggregate
cost/outcome, and `.fabri/logs/<session_id>.log` for DEBUG-level detail. Name
the session ID in the delivery output so a reviewer isn't stuck re-reading the
final chat message as the only evidence a run happened.
