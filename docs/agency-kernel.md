# Agency kernel

An agency is a small product convention for fabri, not a new execution mode.
It makes a repeatable deliverable reviewable by separating the fixed control
path from the decisions that belong to one domain. The convention is designed
for work whose specialist roles are known before a run; use dynamic
`spawn_subagent` only when the task determines the child count or shape.

## The fixed kernel

```text
intake -> inspect -> plan -> delegate -> execute -> verify
                                      ^              |
                                      |--- repair ---|
                                             |
                                      approve -> deliver -> learn
```

The labels are an operating procedure, not new API calls:

- **Intake** turns a request into one bounded deliverable, inputs, proof bar,
  and approval gate. The agency-builder frame makes missing product decisions
  explicit instead of asking a model to guess them.
- **Inspect and plan** happen in the parent agent's normal ReAct loop. For a
  simple fixed pipeline, the parent prompt names the order. For a larger task,
  `agent.planner` can create topologically ordered items; it is optional.
- **Delegate** exposes known specialists with `tools.agents[]`. Fabri builds
  each entry as an ordinary tool and starts a fresh child agent session with
  its own trace and step budget (`agent.subagent.max_steps` bounds
  `spawn_subagent`, not these static entries — each child uses its own
  config's `agent.max_steps`). The parent sees a concise tool result, not the
  child's whole conversation, and does not currently roll the child's cost
  into its own reported total — see "Observe a run" below.
- **Execute** means a specialist calls built-in or custom tools. Custom tools
  are a JSON manifest and an executable: stdin receives JSON arguments and
  stdout returns JSON. File-touching tools must honor `FABRI_SANDBOX_ROOT`.
- **Verify and repair** pair the deliverable with a deterministic tool or a
  trusted `agent.repair.verify_command`. When repair is enabled, fabri reruns
  the agent with verifier output up to the configured bound; an unchanged error
  signature stops the loop early. Repair is a retry mechanism, not an
  acceptance gate on the CLI's own success/failure: after retries are
  exhausted, fabri returns the parent's last result regardless of whether the
  final verifier check passed. Read the verifier's own `ok`/verdict output to
  know whether the deliverable is actually good — do not treat CLI success as
  proof. A human approval gate remains outside the engine and must be named in
  the agency contract.
- **Outcome and narration are each independently unreliable.** Two live-run
  findings show this concretely, not abstractly: one run reported
  `outcome: "success_with_recovery"` while 13/13 tool calls failed and no
  deliverable was created (`session-notes/live-test-run.log:106,132`);
  another reported `outcome: "success"` with 0/14 tool-call failures while
  the model's own final text narrated ongoing verification failure
  (`session-notes/live-test-run-2.log:70,72,98`). fabri's `outcome` field and
  the model's free-text summary each diverged from ground truth, in opposite
  directions, on the same worked example. Neither is evidence alone. The
  only trustworthy signal today is the deterministic verifier's own machine
  output plus the trace's raw tool-call results -- read those, not either
  summary layer. See "Deliver" below for what this means for the delivered
  message.
- **Deliver** is the verified file plus its path, verdict, and trace/session
  identifier. Do not equate a polished final message with verification.
- **Learn** is fabri's existing post-run memory loop: relevant guidelines are
  retrieved before a run, then tool outcomes and successful runs can be mined,
  deduplicated, and promoted for later related sessions. Retain a stable
  per-agency collection rather than wiping it between related work.

## Observe a run

An agency is not done when it prints a final message; it is done when its
trace and cost are inspectable by someone who was not watching. fabri already
ships this — an agency skips it only if the builder never surfaces it:

- `fabri traces list` — recent session IDs, newest first.
- `fabri traces show <session_id>` — the parent's own full step-by-step
  trace: which specialist tools it called and what each returned. Each
  static `tools.agents[]` specialist runs as a separate child session with
  its own session ID (visible in the parent's tool-call result); show that
  ID separately to see the specialist's own internal steps.
- `fabri traces tail <session_id>` — follow a run live instead of waiting for
  it to finish.
- `fabri report --since 1h` — aggregate cost and outcome (`success` /
  `success_with_recovery` / `incomplete`) across recent sessions; this is the
  number that answers "is this agency actually getting cheaper per run."
- `.fabri/logs/<session_id>.log` — always DEBUG-level; the first thing to
  check when a run's behavior is surprising.

Name the session ID in the agency's delivery output (or its README) so a
reviewer can run these commands without grepping for it. An agency that can
only be understood by re-reading its final chat message has not delivered
observability, whatever its verifier says.

The compact kernel is intentionally not a rigid state machine. A narrow agency
may combine inspect and plan; a deterministic delivery tool can combine execute
and materialization. What must remain visible is the acceptance gate and the
artifact it judges.

## Mapping to fabri today

| Kernel concern | Existing fabri primitive | Consequence |
| --- | --- | --- |
| Parent control path | `run_agent` ReAct step loop; optional planner/decompose roles | Keep the workflow in the parent prompt/config, not in new orchestration code. |
| Fixed specialists | `tools.agents[]` agents-as-tools | Give each role a focused config, fresh trace, and child budget. |
| Variable fan-out | `spawn_subagent` with `parallel_group` | Use only when work items are discovered at runtime. |
| Project action | Built-ins plus manifest-backed executables | Use schemas and small JSON results; tools may be polyglot. |
| Filesystem boundary | `tools.sandbox_root` and `FABRI_SANDBOX_ROOT` | Constrain file tools to the project/agency boundary. |
| Acceptance/retry | `agent.repair` and a host verifier command | Verification is repeatable and bounded, not a model promise. |
| Cost boundary | Parent and per-specialist config step/cost budgets | Only `spawn_subagent` fan-out rolls child cost into the parent's total today; static `tools.agents[]` specialists report their own cost separately — set each config's bounds deliberately and read `fabri report` per session, not just the parent's. |
| Learning | SQLite/Qdrant memory retrieval and trace processing | Keep collection names stable across related deliveries. |

This mapping follows `docs/HOW_FABRI_WORKS.md`, `docs/creating-an-agent.md`,
and `docs/using-fabri-well.md`. In particular, fabri does not currently ship a
first-class agency object, a durable workflow-state machine, an automatic
prompt-file loader in `agent.yaml`, or a human-approval UI. This skill uses
documented configs and tools rather than implying those features exist.

## What varies per agency

The kernel remains fixed. Each agency supplies its own target persona; one
deliverable; specialist roles and prompts; tool manifests and schemas;
artifact paths; sandbox scope; policy; proof metric; verifier; provider;
budget; and memory scope (backend, collection name, retrieval depth,
postmortem recording). Treat all of these as reviewable source files. A good walking skeleton
has two or three specialists and one deterministic output check before adding
parallelism, external services, or autonomous deployment.
