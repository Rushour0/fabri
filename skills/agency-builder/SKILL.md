---
name: agency-builder
description: Build or scaffold a fabri multi-agent AI agency from a plain-English business or workflow idea. Use when asked to "build an AI agency for X", "scaffold a specialist team for X", create a fixed multi-agent pipeline, or turn a target deliverable into fabri agent configs, specialist prompts, JSON-manifest tools, verification, and a runnable example.
---

# Agency Builder

Turn one bounded deliverable into a small, reviewable fabri agency. Treat an
agency as a configuration pattern, not a new fabri runtime: an orchestrator
uses static agents-as-tools for fixed specialists; each specialist uses the
normal step loop and JSON-manifest tools.

## Frame before building

Read [templates/agency-frame.md](templates/agency-frame.md). Ask for every
blank that changes the build: target persona, exactly one deliverable,
specialist roles, proof-bar metric, and approval gate. Do not quietly invent
those decisions. If the user supplies a thin idea, return the frame and wait.

## Build the smallest viable agency

Read [references/apply.md](references/apply.md), then create the files it
names. Keep the fixed kernel in [references/agency-kernel.md](references/agency-kernel.md):

1. Make one orchestrator `agent.yaml` with a bounded step budget and only the
   tools and specialist names it needs.
2. Make one config and one explicit prompt for each specialist. Declare fixed
   roles in `tools.agents[]`, so the parent sees them as ordinary tools.
3. Add a JSON manifest plus executable for each project-specific tool. Read one
   JSON object on stdin, print one JSON object on stdout, and test it through
   `fabri tool test` before giving it to an agent.
4. Put the proof-bar in a deterministic verifier. Enable the optional fabri
   repair loop only when a host command can check the deliverable repeatedly.
   `outcome`/`success`/`success_with_recovery` and the model's own final-message
   narration must NEVER be treated as evidence a deliverable exists or is
   correct — two live runs showed 13/13 tool-call failures reported as
   `success_with_recovery` with no deliverable created, and separately 0/14
   failures narrated by the model's own final text as persistent, ongoing
   failure. Both signals are independently unreliable; only the deterministic
   verifier's raw output is trustworthy.
5. Use `memory.backend: sqlite` for a self-contained first run. Give each
   agency its own collection and keep related runs on that collection.
6. Set the frame's cost ceiling as `agent.max_cost_usd` in `agent.yaml` (fabri
   ends a run at `Outcome.BUDGET_EXCEEDED` once crossed). An agency with no
   ceiling is a decision to make explicitly, not by omission — do not leave it
   unset silently. Cost, `cost_by_model`, and run metrics are already emitted on
   the `usage` event and rolled up by `fabri report`; the frame decides the
   ceiling and what to report, not new instrumentation.

## Ship a front-end: Fabri Studio

Do not hand-roll a bespoke UI per agency. Point the reusable **Fabri Studio**
(`examples/studio/`) at the agency's `agent.yaml` over `fabri serve`:

- Single-deliverable / conversational agencies: run `fabri --config
  <agency>/agent.yaml serve`, then the Studio dev server. Studio streams the
  run's plan timeline, tool calls, `ask_user` questions, and a live COGS panel
  (per-model cost + budget) — the frame's reported metrics surface for free.
- Fan-out agencies (one request → N per-item pipelines): use Studio's **Fleet**
  view. `POST /fleets` fans the batch out to N runs; the roll-up shows
  deployed/blocked/running counts and the **summed fleet COGS** (which works
  around the static-specialist rollup caveat by summing per-session). Drill into
  any item to watch its pipeline read-only.

Only build a bespoke dashboard when the agency needs a view Studio genuinely
cannot express; the default is to adapt Studio, not replace it.

Run `fabri --config <agency>/agent.yaml run --dry-run "<task>"` before a live run.
Run the real command only with the configured provider key present. Never claim
an agency ran if the live provider call was not made.

## After a run: report the session, not just the deliverable

A run is only as observable as what you point the user at afterward. Once a
live run finishes:

This is not only builder debugging discipline — bake it into the scaffolded
agency's own delivery gate. Before reporting a run done to anyone outside this
build, run the deliverable's own verifier command directly and quote its raw
output in the delivery message. Do this even when `outcome` says success:
`outcome` has been observed to say `success_with_recovery` on a run where
13/13 tool calls failed and no deliverable existed. The delivery message must
state the verifier's own `ok`/verdict output — not the CLI `outcome` field,
not the model's own prose summary.

1. Note the session ID fabri prints; put it in the agency's delivery message
   or README, not just in your own scrollback. Static `tools.agents[]`
   specialists each run in their own child session with their own ID
   (visible in the parent's tool-call result) — note those too if you need
   to show a specialist's own steps, not just what it returned to the parent.
2. Run `fabri traces show <session_id>` on the parent and quote which
   specialists it called and what they returned — not what the plan intended,
   and not the specialists' own internal reasoning, which lives in their own
   session traces.
3. Run `fabri report --since 1h` and report actual COGS, not an estimate. The
   delivery message must quote: per-run total cost, `cost_by_model`, `outcome`,
   the tool-failure rate, and the cost ceiling it ran under (spent vs
   `max_cost_usd`). Static `tools.agents[]` specialist cost does not roll into
   the parent's `total_cost_usd` — report per-session and sum, or read the sum
   off Studio's fleet roll-up. Reporting COGS is part of the delivery gate, not
   a nicety: an agency whose cost you cannot state is not shippable.

See [references/agency-kernel.md](references/agency-kernel.md)'s "Observe a
run" section for the full command set (`traces list`/`tail`, log file path).
Do not summarize a run from the final chat message alone.

## Common cases

- **The idea is thin.** Return the frame from templates/agency-frame.md and
  wait — do not invent the target persona, deliverable, or approval gate.
- **The verifier fails after a repair attempt, OR a run reports success
  without the verifier having been re-run.** Report the exact verifier output
  and stop; do not raise `agent.repair.max_attempts` to force a pass, and do
  not report a run done on `outcome: success`/`success_with_recovery` alone
  without independently re-running the verifier and quoting its output.
- **No provider key is set.** Run and report the `--dry-run` inspection only;
  say plainly that no live call was made rather than describing a hypothetical
  run.
- **A second, unrelated agency is wanted.** Start a new frame and a new
  `examples/agencies/<name>/` directory with its own memory collection — do
  not extend an existing agency's specialists to cover a second deliverable.

## Boundaries

Do not modify fabri's core loop to implement an agency. Do not call a role a
verifier merely because its prompt asks it to self-check: pair a concrete output
with a deterministic check or a clearly documented human approval gate. Do not
add a specialist when a normal tool call is enough.
