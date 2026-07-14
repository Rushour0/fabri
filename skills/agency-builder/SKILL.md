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
5. Use `memory.backend: sqlite` for a self-contained first run. Give each
   agency its own collection and keep related runs on that collection.

Run `fabri --config <agency>/agent.yaml run --dry-run "<task>"` before a live run.
Run the real command only with the configured provider key present. Never claim
an agency ran if the live provider call was not made.

## After a run: report the session, not just the deliverable

A run is only as observable as what you point the user at afterward. Once a
live run finishes:

1. Note the session ID fabri prints; put it in the agency's delivery message
   or README, not just in your own scrollback. Static `tools.agents[]`
   specialists each run in their own child session with their own ID
   (visible in the parent's tool-call result) — note those too if you need
   to show a specialist's own steps, not just what it returned to the parent.
2. Run `fabri traces show <session_id>` on the parent and quote which
   specialists it called and what they returned — not what the plan intended,
   and not the specialists' own internal reasoning, which lives in their own
   session traces.
3. Run `fabri report --since 1h` and report actual cost, not an estimate.
   Note that static specialist cost does not currently roll into the parent's
   total — report per-session, not just the parent's number, if the agency
   has specialists.

See [references/agency-kernel.md](references/agency-kernel.md)'s "Observe a
run" section for the full command set (`traces list`/`tail`, log file path).
Do not summarize a run from the final chat message alone.

## Common cases

- **The idea is thin.** Return the frame from templates/agency-frame.md and
  wait — do not invent the target persona, deliverable, or approval gate.
- **The verifier fails after a repair attempt.** Report the exact verifier
  output and stop; do not raise `agent.repair.max_attempts` to force a pass.
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
