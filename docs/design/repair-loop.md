# Design note: generic verify → repair → bounded-rerun loop

Status: **implemented** (B8), behind `agent.repair.enabled` (default false).
Author scope: fabri runtime, not any specific host.

> Implemented as the recommended option **(B)**: the loop lives in
> `core/agent.py` as `_run_with_repair`, wrapping the single-attempt engine
> (`_run_single_attempt`). `run_agent` is a thin wrapper that runs once and,
> only when `repair["enabled"]`, enters the loop. When repair is disabled the
> path is byte-identical to before this card. The entrypoint wiring (cli /
> runner / mcp) is deferred per the deliverable plan below; today a host opts
> in by passing the `agent.repair` config block to `run_agent(repair=...)`.

## Config (implemented)

```yaml
agent:
  repair:
    enabled: false          # master switch; false => zero behaviour change
    max_attempts: 2         # cap on RE-RUNS (initial run is attempt 0)
    verify_command: null    # list argv OR a shell string; null => use the run's
                            # own failure outcome as the signal
    verify_cwd: null        # where the verifier runs (default: process CWD).
                            # The verifier is trusted host code, NOT sandboxed.
    stop_on_no_progress: true   # abort early when the error signature is
                                # unchanged between attempts
    repair_prompt: null     # override the built-in neutral instruction;
                            # `{errors}` is interpolated with the verifier output
```

A verdict is `{ok, output}`: exit 0 (or stdout JSON `{"ok": true}`) is ok;
nonzero exit (or `{"ok": false}`) is a failure whose combined stdout+stderr
becomes the `{errors}` injected into the next attempt. **No-progress stop:**
the error signature is a sorted, line-number-stripped hash of the verifier
output; if it matches the previous attempt's signature and
`stop_on_no_progress` is true, the loop aborts (logged as `repair_aborted`,
reason `error_signature_unchanged`) rather than burning the rest of the budget.
Every attempt shares one `session_id`, and the loop emits ad-hoc
`repair_attempt` / `repair_aborted` trace events (no new `events.py` vocabulary)
alongside each attempt's own terminal `final`/`failed`/`incomplete` event.

## Problem

Several hosts that consume `fabri.cli run` have built the same wrapper on
top of it:

1. Run the agent on a task.
2. Run a host-supplied verifier (a linter, type checker, schema validator,
   game-build pass, content-policy check, …).
3. If the verifier reports failure, re-run the agent on top of the current
   state (files / external store untouched) with the verifier's output
   injected as the new prompt.
4. Bound the loop at N attempts; stop early if the error set didn't change
   between attempts (the agent is no longer making progress).

Each host re-implements this in its own dispatch layer. The loop mechanics
are identical; only the verifier and the repair-prompt copy differ.

A reusable fabri primitive would own the **loop mechanics only**. The
verifier and the prompt are host-specific and MUST stay host-supplied —
fabri must not learn any host's domain (lint rules, build pipeline,
content guidelines).

## Out of scope (deliberately)

- The verifier itself. Fabri runs an arbitrary subprocess and reads its
  stdout / exit code; it does not know what the verifier is checking.
- Auto-detecting "is this a code repo / a doc / a game build". The host
  picks the verifier.
- Cross-attempt memory mining. The existing trace-mining pipeline already
  handles "what did the agent learn across runs"; the repair loop is
  within-task tactical, not strategic.

## Proposed config shape

```yaml
agent:
  repair:
    # When unset, the loop is disabled (back-compat default).
    verify_command: ["python", "tools/check.py"]
    # Optional working dir; defaults to the agent's sandbox_root.
    verify_cwd: "."
    # Cap on repair attempts. The original run is attempt 0; this caps
    # the number of re-runs, so max_attempts: 2 means up to 3 total LLM
    # runs (1 initial + 2 repairs).
    max_attempts: 2
    # Format string interpolated with the verifier's stdout. `{errors}` is
    # the only supported placeholder for v0.8; more (e.g. {previous_diff})
    # are easy to add later.
    repair_prompt: |
      The verifier rejected your last attempt. Fix only these issues,
      building on the current files — do NOT re-do the whole task:

      {errors}
    # Identity-of-error signature: how we decide "did the error set
    # change?" so a stuck agent stops burning attempts. Defaults to a hash
    # of the verifier's stdout lines after stripping pathnames/line-numbers
    # the agent's edits would shift. Hosts can override with a regex.
    # signature_regex: "^(.*?):[0-9]+: (.*)$"   # path:line: message
```

## Loop semantics

Pseudocode (within the existing `run_agent` boundary, after the terminal
event of the initial run):

```python
attempt = 0
prev_signature = None
while attempt < repair_cfg["max_attempts"]:
    verdict = run_verifier(repair_cfg["verify_command"])
    if verdict.ok:
        break  # done
    signature = error_signature(verdict.stdout, repair_cfg)
    if signature == prev_signature:
        log_event(session_id, {
            "type": "repair_aborted",
            "reason": "error_signature_unchanged",
            "attempt": attempt,
        })
        break  # stuck — abort instead of burning more LLM time
    prev_signature = signature
    attempt += 1
    log_event(session_id, {
        "type": "repair_attempt",
        "attempt": attempt,
        "errors": verdict.stdout,
    })
    # Re-enter the agent loop with the repair prompt; the message history,
    # tools, memory store all stay the same. The agent sees the current
    # state of the filesystem because tools read from disk.
    repair_task = repair_cfg["repair_prompt"].format(errors=verdict.stdout)
    _rerun_within_session(repair_task)
```

A verifier verdict is `{ok: bool, stdout: str}` derived from:

- exit code 0 → ok
- exit code != 0 → not ok
- a stdout that parses as JSON with `{"ok": true|false}` overrides the
  exit-code interpretation (some verifiers exit 0 but report failures in
  the body).

## Open questions (call out so design review can resolve before coding)

1. **Where does the loop live?** Two options:
   - **(A)** Inside `run_agent`. Pros: every consumer (cli, runner, MCP,
     hosted) gets it free. Cons: `run_agent` already does a lot; this
     adds a second outer loop that interacts with the cost-budget and
     memory-mining pipeline.
   - **(B)** A new `run_agent_with_repair` wrapper in
     `core/repair.py`. Pros: surgical; doesn't touch the existing loop's
     event shape. Cons: every entrypoint (cli, runner, mcp) has to
     opt into calling the wrapper.

   Recommendation: **(B)**. The repair loop is conceptually a host
   concern lifted into framework code — it deserves its own seam, and a
   host that doesn't want it (e.g. a chat front-end) shouldn't pay the
   conditional cost in the hot loop.

2. **Cost-budget composition.** A host that sets `agent.max_cost_usd: $2`
   today is bounding ONE run. Does a 3-attempt repair loop bound at $2
   per attempt or $2 total? Default should be **total** (the host's
   COGS ceiling doesn't care which attempt burned it). Implementation:
   thread the parent's remaining cost budget into each re-run.

3. **Memory mining timing.** `process_trace` currently runs once per
   `fabri.cli run`. With a repair loop, should mining run once per
   attempt (more guidelines, possibly redundant) or once per top-level
   run (fewer, but the within-run signal is lost)? Recommendation: once
   per top-level run; the attempts are mined as a unit with the failed
   attempts marked as `repair_attempt` events so trace mining can learn
   "this kind of error keeps recurring."

4. **Trace event vocabulary.** New events:
   - `repair_attempt` (attempt #, error excerpt)
   - `repair_aborted` (reason: max_attempts / error_signature_unchanged)
   - The terminal `final` / `incomplete` event on the LAST attempt is the
     run's outcome — earlier attempts emit their own terminal events
     under the same session_id, so trace readers see a sequence of
     attempts within one session.

5. **Verifier sandboxing.** The verifier runs as a subprocess in the
   parent's environment (NOT under the agent's `sandbox_root`). It's
   host-supplied trusted code, same trust class as `tools.manifest_dir`.
   Document this explicitly.

## Deliverable plan

1. Land this design note (v0.7.5 — done).
2. Open a PR with `run_agent_with_repair` in `core/repair.py`, the config
   block, and the new trace event types. Behind a feature flag in
   `agent.repair.enabled` (default false) for one release so hosts can
   smoke it without changing config-loading semantics.
3. After one release of real-world use, fold the wrapper's entrypoint
   call into `cli.py` / `agent_runner_tool.py` / `mcp_server.py` so the
   `agent.repair` config block "just works" everywhere.

## Why not just leave this to hosts?

Two reasons it's worth owning:

- **Stop-when-stuck is non-trivial.** Naive "retry N times" wastes
  budget; the error-signature comparison is the actual valuable
  primitive, and getting it right once in fabri beats getting it
  approximately right in every host.
- **Trace fidelity.** When the loop runs inside fabri, every attempt
  shares a session_id and lands in one JSONL — `fabri traces show` and
  the trace-mining pipeline see the whole arc. When the loop lives in
  the host, each attempt is a separate `fabri.cli run` invocation with
  its own session_id, and the cross-attempt signal is lost.
