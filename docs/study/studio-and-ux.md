# Fabri Studio: the face of the engine

Fabri's core engine (agent loop, memory, company compiler) is invisible unless
something renders it. Studio is that something: a conversational, observability-first
web UI that sits on top of `fabri serve` and turns a run's trace events into a
readable, interactive experience — a chat thread with a manager, a live cost meter,
a question card when the agent needs a human, and (eventually) a whole animated
"little company" of sub-agents at work.

This doc walks through what got built, why, and what shape it's in today. Source
lives in two places: the React/Vite frontend at `examples/studio/`, and the Python
backend it talks to at `src/fabri/service/`.

---

## 1. Why Studio exists

Before Studio, the only way to watch a fabri run was to tail a JSONL trace file or
read stdout from `fabri run`. That's fine for debugging the engine, but useless for
actually using an agency — you can't answer a mid-run question from a terminal log,
you can't see cost accumulate, and you definitely can't hand a non-technical person
a JSONL tailer and call it a product.

Studio's job is to be the thing you'd actually put in front of a user: submit a
task, watch it stream, answer the agent when it asks you something, see what it
cost, and come back later to see what happened. It's explicitly **not** a hosted
product — the README calls it "a copy-and-adapt template" — but it's a full
reference implementation good enough to demo a real agency in.

## 2. The core event contract

Fabri agents don't emit prose to a UI; they emit typed trace events (`start`,
`plan_started`, `tool_started`, `tool_call`, `thought`, `narration`, `final`,
`ask_user`, `usage`, `failed`, …) as JSONL lines, defined in `src/fabri/events.py`.
Studio's entire frontend is built on translating that vocabulary into a small set
of message classes so a run reads like a conversation instead of a log.

That translation lives in one file: `examples/studio/src/lib/events.ts`, function
`classify()`. It's worth reading directly because it's the single source of truth
for "what does this event look like on screen":

| fabri event | Studio class | what it renders as |
|---|---|---|
| `final` | `manager` | the primary, full-weight bubble — the agent's answer |
| `narration` | `narration` | a light "what's happening now" caption line |
| `thought` | `thought` | a collapsible reasoning disclosure |
| `plan_started`/`plan_item_*`/`plan_finished` | `plan` | a live step timeline ("step 2 of 5") |
| `tool_started` + `tool_call` | `tool` | a tool-call card, paired by `step:call_index`, with args/result/duration |
| `spawn_subagent` / `parallel_group_started` | `tool` | activity chip ("Delegating to a sub-agent") |
| `ask_user` | `ask` | the interactive question card — full weight, amber-adjacent |
| `usage` | `cost` | the COGS panel: total, per-model, tokens, steps, sub-agent split |
| `cost_unaccounted`/`discrepancy` | `warning` | a flagged risk to spend/truth |
| `structured_output` (invalid only) | `note` | a quiet schema-retry note |
| `failed`/`incomplete`/`error` | `terminal` | a full-width run-ending rule |

Two design decisions worth calling out:
- **Hierarchy by container weight, not color.** The 2026-07-14 design pass
  (`731e92b`, run through the `/agentic-design` skill) deliberately made the visual
  weight of a message class carry meaning that survives grayscale: manager message
  and ask-card are the only full cards, narration is a bare caption, thought is a
  left-rail disclosure, cost is an "attached receipt." Color is reserved for one
  thing — a single amber accent meant "needs you" (the ask card). This was later
  swapped to blue (`9d92f4`) in a rebrand pass (`9d99e6f`), but the weight-not-color
  principle stayed.
- **Termination is keyed off the SSE `event: result` frame, not stream EOF or the
  subprocess's stdout JSON envelope.** This was a deliberate choice tied to earlier
  work on outcome-trust (see `fabri-outcome-trust-gap-fix` memory) — trusting stdout
  JSON alone had already burned the project once (a guideline-synthesis note printed
  to stdout broke JSON parsing for every successful run that synthesized a
  guideline; fixed in commit `2747c77`).

## 3. The ask_user-over-serve HITL flow — the hard part

The single hardest piece of backend work in Studio wasn't the UI — it was closing
the human-in-the-loop gap between `fabri serve` (a stateless HTTP process) and the
`ask_user` tool, which was originally written assuming a human was sitting at a
terminal reading stdin.

**The mechanism** (`src/fabri/service/ask_user_listener.py`, 252 lines):

1. Before launching a run, `FabriService.submit()` starts an `AskUserListener` — a
   per-run `AF_UNIX` socket server (kept short and un-nested under `/tmp/fabri-ask/`
   because macOS caps Unix socket paths at ~104 bytes).
2. The listener's socket path is injected into the agent subprocess via the
   `FABRI_ASK_USER_SOCKET` env var. Because subprocess env is inherited, this also
   reaches any nested `spawn_subagent` children — a parallel step's sub-agents can
   all ask questions through the same bridge.
3. When the `ask_user` tool fires, it connects to that socket and blocks. The
   listener's `on_question` callback does the load-bearing trick: it appends a new
   `ask_user` trace event (`EventType.ASK_USER`, previously defined but unused)
   directly to the run's own trace JSONL, via a path-explicit `append_trace_event`.
   That means the question rides the *existing* SSE stream the browser is already
   tailing, and — critically — it carries the tool's `question_id`, which the
   ordinary `tool_started` frame does not.
4. The connection stays open (a generous 3600s wait, matching the tool's own client
   timeout) until `POST /runs/<id>/answer` calls `FabriService.answer()`, which
   writes the reply back down the held socket connection, unblocking the tool call.
5. The browser sees an `ask_user` event → renders `AskUserCard.tsx` (options as
   pills, free-text fallback) → the Composer posts the answer → the run continues
   streaming as if nothing happened.

Concurrency is handled by keying pending questions by `question_id` in a dict
(`_Pending`), so multiple sub-agents in a parallel group can each have a live
question outstanding at once.

**Real-LLM testing surfaced real bugs.** Driving `fabri serve` against an actual
OpenAI model (not the test suite's fake-agent scripts) found and fixed three
pre-existing bugs in one commit (`2747c77`):
- `launcher.build_run_command` put the global `--config` flag *after* the `run`
  subcommand, so every serve-launched real run died with argparse exit 2.
- The "Synthesized N guideline(s)" note printed to stdout, breaking JSON parsing of
  the result envelope for every successful run that synthesized a guideline (moved
  to stderr).
- `RunHandle.result` was hardened to `raw_decode` just the first JSON object off
  stdout instead of assuming the whole stream was one JSON blob.

This is a good example of a pattern that recurs through fabri's history: the test
suite (fake agents, mocked LLMs) is necessary but not sufficient — several
load-bearing bugs only showed up once a human ran the thing against a real model
in a real browser.

## 4. Four surfaces, one shell

`App.tsx` (399 lines) is a shell over four tabs, URL-tracked via a hand-rolled hash
router (`useHashRoute.ts`, no react-router dependency) so tabs are deep-linkable and
survive browser back/forward:

- **Conversation** — the live thread. A task submission opens a turn; a follow-up in
  the same thread shares a `thread_id` and a bound `memory.collection` override,
  plus a transcript preamble carried in the task text (memory alone was judged
  unreliable for raw conversational continuity — a flagged risk called out
  explicitly in the memory note, not silently assumed to work). Stop/cancel and
  retry live in `Composer.tsx`.
- **Company** — described below (§5).
- **Fleet** — a whole-agency view: paste one task per line, `POST /fleets` fans it
  out to N runs sharing a `fleet_id`, and `FleetView.tsx` polls `GET /fleets/<id>`
  every 2s while anything is running, rendering a roll-up header (done/running/error
  counts, summed COGS, per-model breakdown) plus an `AccountTile` grid for
  drill-down into any one run.
- **History** — every past run, backed by a durable index (§6) that survives a
  `fabri serve` restart. Opening one replays it read-only through `RunReplay.tsx`,
  which also gets a Timeline↔Company toggle.

An early bug here (`9d99e6f`) was the app frame literally changing width
(720px↔1440px) when switching tabs, which read as the conversation column
"shrinking" every time you left and came back. Fixed by making the outer frame
always full-width and letting the conversation surface center its own narrower
column inside it — switching tabs no longer resizes anything. A related fix
(`ba5ddca`) widened the Questions inbox surface, which had been left off the
wide-layout whitelist and was snapping back to the narrow conversation column.

## 5. The Company view — agents as characters

Built 2026-07-18, merged via PR #18. This is the most visually ambitious surface:
a run rendered as a "little company" — the manager and its sub-agents as
emoji-avatar characters, task handoffs animating between them, and a live payroll
(COGS) counter ticking as work happens. Two skins toggle over one shared data
layer: **🏢 Office** (characters that light up idle→running→done/error, a 📄 icon
that animates along each handoff) and **🕸 Org chart** (node cards with a status
pill, cost, and token counts; edges labeled by the handed-off task).

**Key architectural decision: entirely client-side.** No backend or Python changes,
no new npm dependencies (no react-flow — hand-rolled CSS/SVG). The pure function
`buildAgencyGraph(events)` in `examples/studio/src/lib/graph.ts` derives nodes,
edges, and payroll straight from the SSE trace events Studio was already streaming
for the Conversation tab. The load-bearing insight documented in the code comment:
a `tool_call` counts as an agent handoff if it's a dynamic `spawn_subagent` call, OR
its result carries a nested child `session_id`/`trace_path` (a static
`tools.agents[]` specialist) — so one level of the org graph needs no extra fetch,
because the child's session_id/outcome/usage ride inside the parent's own
`tool_call` result.

**The nested-envelope bug (caught by Codex review, gpt-5.6-sol).** The first
implementation was built and browser-verified against a hand-authored mock `fabri
serve`, and passed. A subsequent Codex review caught that the mock's shape didn't
match the real producer: a tool_call's `result` field is the canonical envelope
`{ok, result, error}` defined in `src/fabri/tools/result.py`, and a sub-agent's own
return payload (session_id, outcome, usage) is nested **one level down**, under
`result.result` — not flat on `result`. Reading `ev.result` directly worked fine
against the flat mock but silently broke on real traces: static specialists
vanished from the graph entirely, and dynamic nodes showed $0 cost with the wrong
outcome. Fixed in commit `600314c` by splitting the read into two explicit helpers
in `graph.ts`:

```ts
// A tool_call's `result` is the canonical envelope {ok, result, error}
// (src/fabri/tools/result.py). A sub-agent's own return — session_id, outcome,
// usage, … — is nested one level under `result`.
function envelope(ev) { return recordValue(ev?.result); }
function childResult(ev) {
  const env = envelope(ev);
  return recordValue(env?.result) ?? env;  // prefer nested, fall back flat
}
```

Same commit also fixed a status-latch bug (a verify→repair→verify loop that
ultimately succeeded was staying stuck red from its first failed attempt — node
status now reads off the *latest resolved* call, while per-call edges still keep
each attempt's individual pass/fail) and namespaced specialist node ids as
`agent-<slug>` so a literally-named "Manager" sub-agent can't collide with the
reserved root node id.

**The general lesson**, stated directly in the memory note and worth keeping: when
browser-verifying against a self-authored mock, the mock's shape must be checked
against the real producer's source. A flat mock passed cleanly while real nested
traces broke — the test gave false confidence precisely because the test author and
the mock author were the same (fallible) source.

A follow-up fix (`525e8a8`, 2026-07-23) made the org chart expand a "crew" leaf node
into its individual member roles instead of rendering the whole crew as one box,
reusing `compile_company`'s own agency-resolution logic so it fails open to the flat
chart if a crew's agency dir can't be resolved.

## 6. The service layer that feeds it all

`src/fabri/service/` is the Python backend Studio (and any other frontend) talks to.
It's designed to be transport-agnostic — `FabriService` is a plain Python class with
`submit`/`stream`/`result`/`cancel`/`answer`, and `http_server.py` is a thin HTTP/SSE
wrapper around it. There's even a stdio JSON-lines transport (`serve_stdio`) for a
non-Python host to drive it without any fabri imports.

**Per-run lifecycle**: `submit()` creates a fresh `FABRI_HOME` under
`home_root/<session_id>`, binds the per-run config (`binding.py`), starts an
`AskUserListener`, launches the agent subprocess (`launcher.py`), and records a
`submit` line to an append-only `home_root/index.jsonl`. `stream()` tails the
run's trace file live via `tailer.py`. `result()` blocks for the subprocess, closes
the ask_user bridge, reads the completed trace for a cost surface
(`extract_cost`), and writes one terminal index line.

**SSE event shapes**: each trace JSONL line becomes one SSE frame; the browser's
`EventSource` in `useRunEvents.ts` accumulates them into `turns[]` (a thread of
runs). The terminal frame is fabri's own named `event: result`, carrying a result
envelope: `{session_id, success, outcome, final_text, structured_output, usage,
cost, error}`.

**Replay of persisted runs after a restart (PR #81, commit `2c3dbaa`, merged
2026-07-23).** Studio's History list survived a `fabri serve` restart from the
start (backed by the durable `index.jsonl`/`RunStore`), but clicking into an old
run to replay its events did not — `stream()` and `result()` only knew about
in-memory `RunHandle`s, so a restart raised a `KeyError` ("couldn't replay this
run") for anything the process didn't personally launch. The fix adds a fallback
path in both methods: if there's no live handle, check whether the durable
`RunStore` knows the session; if so, reconstruct its trace path from the launcher's
own per-run-home convention and either tail it to EOF (`stream()`, with an
always-false `is_running` predicate since it's historical) or rebuild the result
envelope directly from the trace's terminal/usage events (`result()`, via a new
`_persisted_result()` that hunts backward through the trace for the last
`final`/`failed`/`incomplete` event and the last `usage` event). Sessions the store
has no record of still hit the old `KeyError` → HTTP 404 path; only genuinely
known-but-cold sessions get replayed. Gate: 1403 passed, 1 skipped.

**AGENT_MEMORY block leaking + stripping (PR #80, commit `525e8a8`, merged
2026-07-23).** Two bugs reported from live AgentWorks runs, both traced to
`src/fabri/memory/output.py`'s `split_agent_output`, the function that separates a
model's human-facing prose from its machine-readable memory block:
1. The model emitted the block as `<!-- AGENTMEMORY -->` (an HTML comment, no
   underscore) instead of the canonical form, and the exact-string partition logic
   missed it entirely — so the raw memory block leaked straight into the
   user-visible answer in Studio.
2. Fixed by making the marker a real tag pair, `<AGENT_MEMORY>...</AGENT_MEMORY>`,
   with a tolerant regex (`_AGENT_MEMORY_OPEN_RE`/`_AGENT_MEMORY_CLOSE_RE` in
   `output.py`) that matches case-insensitively, with or without the underscore/a
   space, the legacy HTML-comment form, and even a missing close tag. Output with no
   marker at all stays byte-identical, and the block stays optional — an earlier
   attempt to make it a *hard* output contract (commit `4ab8cb1`, "make the
   AGENT_MEMORY block a hard output contract," release 0.19.3) was reverted one
   release later (`482e9aa`, "Revert," release 0.19.4) because it apparently caused
   more problems than it solved. That revert-and-retry is itself useful history:
   the fix that shipped is deliberately *tolerant* stripping rather than a strict
   contract, a direct lesson from the earlier hard-contract attempt failing.

**Manager delegation call timeouts (PR #63, commits `3c9edf4` + `6a23a64`, merged
2026-07-21).** In a multi-level company (root → director → crew → specialists), a
manager's delegated call blocks until its *entire subtree* resolves — but every
manager-to-child call was using `agent_tool`'s generic 120-second
`DEFAULT_TIMEOUT_S`. Observed failure: both directors in a 6-crew studio got killed
at 120s before any leaf agent could finish. Fix: `compile_company` now injects an
explicit `timeout_s` into every manager child entry, resolved in priority order —
per-node `timeout_s` override, else `[company].call_timeout_s`, else a 900-second
default. A same-day follow-up review commit hardened this against bad input:
`load_company` now rejects a non-numeric, non-positive, non-finite, or boolean
`call_timeout_s`/`timeout_s` with a clear `CompanyError` at load time, instead of
letting a bad value serialize into the compiled YAML and fail late inside
`subprocess.communicate(timeout=...)`. Regression tests cover the 900s default, the
company-level override, per-node precedence, and invalid-value rejection.

## 7. Same-origin distribution: `fabri studio`

Shipped in commit `669c623`, released as part of 0.14.0. The problem: Studio as a
dev-only Vite app (`npm run dev`, proxying to a separately-running `fabri serve`)
is fine for development but useless for distributing fabri as an installable
package — nobody wants to clone the repo and run two dev servers to see the UI.

The fix is `fabri studio --config <agency>`: it runs the exact same `FabriService`
as `fabri serve`, but with static file serving switched on. `http_server.py` serves
the pre-built Studio assets (bundled into the wheel at
`src/fabri/service/studio_assets/`, currently an `index.html` + hashed
`index-*.js`/`index-*.css`) at `/` with SPA fallback, while the run API (`/runs`,
SSE, `/fleets`, `/health`) is served from the exact same origin — so there's no CORS
concern and no Vite proxy needed in production. Static serving is strictly opt-in
(a `serve_studio` flag, default off), so plain `fabri serve` behavior is completely
unchanged. API routes take precedence over the static file fallback, and a missing
asset directory gives an actionable CLI error message rather than a stack trace.

CI and release workflows were extended in the same commit to build
`examples/studio` with Node, sync the built assets plus example configs into the
wheel's data files, strip stray `__pycache__` directories, build the wheel, and
assert on its contents (studio index + assets present, example dotfile present, no
stray `.pyc` files) — so a broken frontend build or a missed asset-sync step fails
CI instead of shipping silently broken.

## 8. Current state: proven vs. open

**Proven / shipped / merged to main:**
- Conversational core UI + event→message-class mapping — PR #11/#12, live-verified
  against real OpenAI `gpt-4o-mini`.
- ask_user-over-serve HITL round-trip — live-verified end-to-end in browser.
- Fleet mode, live COGS panel, multi-turn threads, run history, cancel/retry — PR
  #13, live-verified against real OpenAI post-merge (empty state, streaming
  conversation, ask card with option pills, tool cards, COGS panel with per-model
  breakdown, fleet roll-up with 3 accounts, 6-session history — all browser-checked,
  zero console errors).
- Company/agency-graph view (Office + Org chart skins) — PR #18, merged, static
  build/typecheck green, browser-verified against a mock `fabri serve` (both a live
  parallel fan-out and a replayed static repair loop).
- The nested-envelope bug fix, status-latch fix, and node-id namespacing —
  commit `600314c`, caught by Codex review before it shipped broken.
- Same-origin `fabri studio` distribution — wheel-verified, live bug-triage run
  streamed into Company view same-origin with 0 console errors.
- Tab-URL hash routing + full-width-frame stability fixes.
- Run replay after a `fabri serve` restart — PR #81, gate: 1403 passed / 1 skipped.
- AGENT_MEMORY tag-based stripping (tolerant of the leaked comment-form marker) —
  PR #80, gate: 1399 passed / 1 skipped.
- Manager delegation timeouts for deep companies — PR #63, with regression tests
  for the default, the override, precedence, and invalid-value rejection.
- Optional email/password auth + per-user run isolation (`45059c7`,
  `22ecd33`/tenant-isolation hardening, `17ea16e`) — not covered in depth in this
  doc but present in the current surface list; guests can still browse and run
  without an account.

**Open / explicitly flagged as unproven:**
- The Company view's original browser verification used a **mock** `fabri serve`,
  not a live LLM — and that exact gap (mock shape drifting from the real producer)
  is what caused the nested-envelope bug to ship in the first place. Treat any
  future Company-view change as needing a real-trace check, not just a mock one.
- Memory continuity for multi-turn conversation threads was judged "unreliable"
  for raw conversational continuity on its own — Studio compensates with an
  explicit transcript preamble in the task text rather than trusting memory
  retrieval alone to carry context turn-to-turn. This is a known workaround, not a
  solved problem.
- The AGENT_MEMORY "hard output contract" idea (require the model to always emit
  the block) was tried once (0.19.3) and reverted one release later (0.19.4) as a
  net negative — worth remembering if the same idea resurfaces, since the current
  tolerant-stripping approach was chosen specifically because the strict version
  didn't work.
- Studio's own README calls itself "a copy-and-adapt template, not a hosted
  product" — there's no plan (as of this writing) to run it as a standing SaaS
  surface; it's a reference implementation you fork per agency.

## 9. Where to start reading

If you want to trace one full round-trip yourself, start here in order:
1. `examples/studio/src/lib/events.ts` — the event→message-class contract.
2. `examples/studio/src/hooks/useRunEvents.ts` — how SSE frames become UI state.
3. `src/fabri/service/service.py` — `submit`/`stream`/`result`, the whole
   run lifecycle.
4. `src/fabri/service/ask_user_listener.py` — the HITL bridge, if you want to
   understand the trickiest mechanism in the whole system.
5. `examples/studio/src/lib/graph.ts` — the Company view's pure data layer, and
   the `envelope()`/`childResult()` split that fixes the nested-result bug.
6. `examples/studio/README.md` — the diagram of the whole `POST /runs` →
   `EventSource` → `POST /runs/<id>/answer` flow, which is worth reading end to
   end even after this doc.
