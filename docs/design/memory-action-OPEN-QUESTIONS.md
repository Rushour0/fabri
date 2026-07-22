# Memory-as-Action — open questions (autonomous loop; user answers later)

The user authorized autonomous building over a few hours: assume the best on open decisions,
keep questions here for later review, don't block. Each item lists the ASSUMED default I'm
building against and how to reverse it if the user disagrees. Revised as research lands.

## Decisions I'm assuming (reversible)

1. **Tier classifier — when does it run?** ASSUME: at **mine-time** (ingest), so drop-tier
   lessons are never stored. Reverse: move to promote-time if research shows post-hoc scoring is
   materially better.
2. **Reconcile with existing `pruning.py` eviction.** ASSUME: tier sits **upstream** — tier
   decides admission + which channel (prompt vs action-store vs drop); `pruning.py` still caps
   the prompt-tier store by tokens/recency. Reverse: fold eviction score into the tier score.
3. **How is the tier assigned?** ASSUME: **hybrid** — deterministic signals first (entry `kind`,
   `tool_verified`, outcome-derived severity, recurrence count); an LLM classification only for
   the ambiguous remainder, to control cost. Reverse: all-LLM or all-heuristic.
4. **Action representation (memory tier).** ASSUME: a structured record
   `{problem_signature, match_predicate, resolution:{type: config_patch|param_override|tool_call,
   payload}}` surfaced to the agent tool loop as a **proposed** applicable fix. Reverse: freeform.
5. **Recurrence / signature match.** ASSUME: error-fingerprint (normalized failure-reason + role
   + error class) AND embedding-similarity above a **conservative** threshold, tuned to bound the
   false-apply rate. Reverse: looser/tighter matching per research Q3.
6. **Auto-apply vs propose.** ASSUME: within-run **auto-apply for deterministic, tool_verified
   config fixes** (behind a flag, every application logged); **propose-only** otherwise. NEVER
   apply on the corrected-control benchmark arm. Reverse: propose-only everywhere.
7. **Part 1 engine benchmark.** ASSUME: use the **synthetic Revenue-Ops truncation** recurrence
   case (max_tokens 768→2048) as the worked mechanism test — no full sales company needed to
   prove the engine. Reverse: build a dedicated synthetic case.
8. **Scope for this autonomous window.** ASSUME: **Part 1 (engine) only** this window — land it
   test-green in the worktree. **Part 2 (sales company in `fabri-rosters`)** is a separate repo;
   defer unless Part 1 finishes with time to spare. Reverse: start Part 2 sooner.
9. **Feature-flag / backward-compat.** ASSUME: all new behavior is **off by default** behind a
   config flag; existing injection path unchanged when the flag is off (keeps PR #71 semantics
   intact and the change reviewable). Reverse: make tiering the default.

## Hard constraints (not assumptions)
- Work stays in worktree `agent/memory-action-research`; do NOT touch PR #71's
  `agent/memory-evolution` branch. Rebase onto `main` after #71 merges.
- Every code change ends at the project's real test gate (green) before it's called done.
- Codex does implementation + research; Opus (me) verifies; Fable reviews plans.

## Research update (2026-07-22, Codex `docs/research/memory-tiering-patterns.md`, citations spot-verified)

Confirmed/refined the assumptions above:
- **#2 upstream of `pruning.py`** — CONFIRMED by research ("sit upstream; do not replace
  `_eviction_score` initially; protected action records live in a separately-capped collection").
- **#3 hybrid classifier** — REFINED to **transparent feature groups** (severity, evidence,
  recurrence/exposure, generality/scope, actionability, inject-cost-vs-value, freshness), NOT one
  opaque LLM judgment. Store score components + reason codes for audit.
- **#1 mine-time** — REVISED to **two stages**: mine-time *admission* (extract signature/scope/
  candidate resolution, reject noise, quarantine unverified actions, admit declarative to
  retrieve) + promote-time *classification* into `core|retrieve|action|quarantine|drop` once
  cross-session recurrence + verification exist. Nothing becomes core/auto-action at mine-time.
- **#4 action record** — CONFIRMED, upgraded to a typed **`ActionMemory`**: `{problem_signature,
  scope, preconditions[], steps[{capability, args_template}], postconditions[], rollback,
  evidence, policy}`; executor resolves logical `capability` → registered ToolRegistry tool and
  submits **synthetic typed calls to the SAME dispatcher** as model tool calls. Never `eval`.
- **#5 recurrence** — CONFIRMED cascade: canonicalize telemetry → exact fingerprint → structured
  near-match (hard fields must match) → embedding recall (candidates only) → applicability
  predicate with `apply_confidence` ≠ retrieval relevance.
- **#6 auto-apply** — CONFIRMED: **shadow → canary → auto**; auto only for verified + exact/high
  structured match + idempotent + verifier + rollback; approval-gated for destructive/external/
  compliance/broad-scope. Track false-apply precision.
- **Tier vocabulary** adopted from research: `core | retrieve | action | quarantine | drop`
  (5-way, replaces my earlier 3-way `prompt|memory|drop`).
- **Q4 = NULL evidence** — no controlled study proves tiering beats full-context injection for
  work agents (closest proxy Mem0: >90% token-cost cut on LoCoMo, but conversational + bundled).
  → SHIP BEHIND AN EVAL FLAG; do NOT claim a quality win; measure it (Part 1 benchmark).

## SCOPE CHANGE (verified against repo, decision-critical)
**Fabri already supports MCP + external tool registration** — `tools.mcp_servers` is
config-declared (`config.py:166`, iterated at build in `runtime.py:272`), `mcp_client.py` does
`tools/list`+`tools/call`, `registry.py` has `register/register_callable/invoke`, manifests via
`manifest_schema.py`+`runner.py`. So **Part 2's "connector layer" is a CLIENT UPGRADE, not
greenfield**: the existing client hard-codes `protocolVersion "2024-11-05"`, stdio-NDJSON + a
custom JSON-RPC POST, static header/env auth — **no OAuth, no Streamable HTTP/SSE, no pagination**
— so it cannot consume modern vendor servers (HubSpot/Apollo/Outreach need OAuth+Streamable HTTP).
Do NOT have Codex build a connector layer from scratch.

## Baseline + rebase record (2026-07-22)
- The PR #71 branch **advanced during this session**: the in-flight WIP was committed as
  `e1834f2` ("engine: memory mining/verification/retrieval WIP…") + `8743fc8`. My worktree was
  created at the then-tip `efef189`, which was transiently **test-RED** (`tests/test_memory_
  evolution.py` imports `MiningReport` that didn't exist there) and had a silent-drop verification
  bug. **Rebased the worktree onto `8743fc8`** (same PR #71 branch, current) → green.
- At `8743fc8` the contract is already satisfied: `MemoryEntry.verification/producer_agent_id/
  source_session_ids` exist + persist; the `verification: any|verified` retrieval filter EXISTS
  (`retrieval.py:106,508-515 verification_allowed`); `MiningReport` exists. (The earlier code-map
  agent read the stale `efef189` and reported these as missing — resolved by the rebase.)
- **Baseline green:** Part-1 area = 163 passed / 5 skipped; whole-suite collection 1168 / 0 errors;
  unit-marked 24 passed. Run offline with `uv run --frozen pytest …` (bare `uv run` rewrites uv.lock).
- **PARKED for user:** PR #71's tip is fine now, but note that commit `efef189` on that branch was
  transiently test-red — worth confirming CI is green on #71 before merge.

## Implementation follow-ups (tracked, not blocking)
- **Increment 1 DONE + committed (`3ae8955`).** tier field + classifier + unconditional quarantine
  exclusion at all 4 retrieval sites + `memory.tiering_enabled` flag; 9 tests; gate 172 passed.
- **Write-side wiring gap:** `ingest_guideline` gained a `tiering_enabled` param (default off) but
  the `pipeline.py` call site doesn't pass it yet, so classification only runs in tests. Wire
  config→pipeline when the feature is actually turned on (deferred to the enablement increment).
- **Increment 2 DONE + committed (`47e823d`).** `resolution` ActionMemory field + pure
  `recurrence.py` (canonicalize/fingerprint/applicable/apply_confidence); 7 tests.
- **Increment 3 DONE + committed (`306d4ba`).** proposed-action shadow surfacing +
  `memory.memory_action_enabled` flag + Revenue-Ops golden test (detect→propose→refuse); 7 tests.
- **Review fixes DONE + committed (`0cd404f`).** Adversarial code-review (code-reviewer agent)
  caught: (a) `propose_actions` unbounded full-store scan → bounded (`kind=success_pattern`,
  limit=200); (b) unguarded store I/O could abort a turn → fail-closed to `[]`; (c) test-vs-truth
  gap — added `test_live_agent_current_state_cannot_match_today` + a source comment. Area gate 244.
- **HONEST STATUS: Part 1 MECHANISM complete + unit-proven (tiering + recurrence matcher + shadow
  surfacing, all flag-off default, 244 area gate green). NOT yet live-functional for the flagship
  Revenue-Ops case:** `_run_single_attempt` can't see company/agency/sibling-role config, so
  config-precondition action detection can't match there. **Detection belongs at the
  ORCHESTRATION/COMPILE layer** (whole compiled agency visible) — that's the next increment to make
  it actually work live. Tiering IS live via retrieval; only cross-agent action-surfacing is pending.

## Questions genuinely needing the user (parked — NOT auto-deciding)
- **Proving the cost/retry win is LIVE-SPEND gated.** The engine benchmark that proves
  memory-as-action reduces cost/retries needs live OpenAI runs against the `fabri-rosters` fixtures
  (the frozen 7-company shared agency must NOT be mutated — only an ephemeral compiled copy). Need:
  (a) authorization + a $ budget for a live run, (b) OPENAI_API_KEY availability. Until then I prove
  the mechanism offline (unit tests), not the $ win.
- **Production vs benchmark path for the orchestration hook.** Options: (1) benchmark-only apply
  (mirror `company_memory_study.py::apply_retrieval_overrides` — rewrite the matched role's
  max_tokens in the ephemeral compiled YAML before `fabri run`), lowest risk, proves the win;
  (2) production shadow at `cli.py::cmd_run` (detect+log, no apply); (3) production auto-apply
  (canary→auto). ASSUMING (1)+(2) first (benchmark proof + production SHADOW only, no auto-apply)
  per the research's shadow→canary→auto rollout. Reverse if you want auto-apply sooner.
- **Increment 2 miner deferred:** nothing lands in the store as an ActionMemory until a miner writes
  quarantine-tier candidates from real recovery traces (touches PR #71's `pipeline.py` mining path).
  Building detection against SEEDED ActionMemory for now; the miner is a separate increment.
- No PR #71-scope changes. Will append blockers here.

## Increment 4 (building now, offline, no spend): orchestration-level detection unit
Pure, tested function `detect_proposed_actions(store, agents_entries, task, *, company, agency)` that
loads each child config, builds a RICH `current_state` (company/agency + roles_config keyed by child
node name → max_tokens), and calls `retrieval.propose_actions`. This closes the review's critical
gap (detection at the layer that actually has the config) WITHOUT production wiring or live runs.
New module `src/fabri/orchestrator/action_detection.py` + `tests/test_unit_action_detection.py`.
