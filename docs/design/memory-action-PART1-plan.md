# Part 1 — Memory-as-Action Redesign (engine) — implementation plan

Base: worktree `agent/memory-action-research` @ `8743fc8` (current PR #71 tip; rebased up from
the stale `efef189`). **Baseline green:** Part-1 area (`-k "memory or retriev or prun or pipeline
or schema"`) = **163 passed, 5 skipped**; whole-suite collection clean (1168 tests, 0 errors);
`unit`-marked = 24 passed / 3 skipped. Run offline with `uv run --frozen pytest …` (the `--frozen`
avoids uv.lock churn). All new behavior is **feature-flagged, default OFF** — byte-identical
behavior when flags are off, so it does not disturb PR #71 semantics and stays reviewable.

## What already exists at the base (do NOT rebuild — verified in source)
- `MemoryEntry` (schema.py) has `verification` (`unverified|tool_verified|rubric_verified|
  contradicted`), `producer_agent_id`, `source_session_ids`, `outcome`, `kind`, `domain`,
  `hit_count`, `dedup_key`; `to_payload`/`from_payload` persist them; `id` = `sha256(namespace(kind)
  + text)` only.
- Verification retrieval filter EXISTS: `RetrievalConfig.verification` (retrieval.py:106) from
  `mem_cfg["retrieval_verification"]` (121); `verification_allowed(entry)` (511–515) excludes
  `contradicted` and, when `"verified"`, requires tool/rubric-verified; ANN-window guard (554–555).
- `MiningReport` dataclass (pipeline.py:30); `process_trace` miner; `ingest_guideline` in
  pruning.py; `_eviction_score` recency×hit_count (protected kinds = `{"strategic"}`).
- Config lives in one nested dict under `"memory": {...}` (config.py); `RetrievalConfig.from_mem_cfg`
  reads it. Tool loop: `ToolRegistry.register/register_callable/invoke`; MCP already wired
  (`mcp_servers` config, `runtime.py`, `mcp_client.py` — dated client, but present).

## Design (from `docs/research/memory-tiering-patterns.md`, citations spot-verified)
Tier vocabulary `core | retrieve | action | quarantine | drop`. Tiering sits **on top of**
verification (verification = trust; tier = value + placement) and **upstream of** `pruning.py`
eviction (do not replace `_eviction_score` yet). Classifier = **transparent feature groups**
(severity, evidence, recurrence/exposure, generality/scope, actionability, inject-cost-vs-value,
freshness), NOT one opaque LLM call. Two stages: mine-time *admission* + promote-time
*classification*. Action memory = typed `{problem_signature, scope, preconditions, steps
[capability→args], postconditions, rollback, evidence, policy}`, executed as **synthetic typed
calls through the same dispatcher** as model tool calls (never `eval`). Recurrence = canonicalize
→ exact fingerprint → structured hard-field match → embedding recall (candidates only) →
applicability predicate. Rollout **shadow → canary → auto** (auto only verified+exact+idempotent+
verifier+rollback). Q4 = null evidence → measure behind a flag, claim no quality win yet.

## Increments (each ends test-green before the next; delegated to Codex one at a time)

### Increment 1 — Tier field + classifier + retrieval filter (flag: `memory.tiering_enabled=False`)
*(Fable-revised: concrete rules, merge-time re-tier, unconditional drop/quarantine exclusion at
all four filter sites, eviction tier-weight deferred.)*
- **schema.py**: add `tier: str = "unclassified"` (values `core|retrieve|action|quarantine|
  unclassified`; **no `drop` value stored** — drop = "not admitted", see below). Persist in
  `to_payload`/`from_payload`; **keep out of the `id` hash** and out of `dedup_key`; legacy payloads
  default via `payload.get("tier","unclassified")`.
- **pruning.py `ingest_guideline`** — deterministic classifier with **concrete rules**, gated by
  the flag (flag off ⇒ tier stays `"unclassified"`, entry always admitted, zero behavior change):
  - `verification == "contradicted"` → **drop** (skip insertion entirely; do not store).
  - unverified + generic/low-value (e.g. bare success summary, one-off stylistic) → `quarantine`.
  - `verification ∈ {tool_verified,rubric_verified}` + `kind=="strategic"` + ≥N distinct
    `source_session_ids` → `core` candidate.
  - has a typed `resolution` (Increment 2) + verified → `action`.
  - default → `retrieve`.
  Run the classifier in **both** the new-entry branch AND the **merge branch** (`pruning.py:295-346`,
  where sessions accumulate / verification upgrades) — at merge, tier may only be **raised**
  (mirrors the existing `verification_rank` upgrade pattern), never lowered.
- **retrieval.py**: define one predicate `entry_allowed(e) = verification_allowed(e) AND
  tier_allowed(e)` and use it at **all four** current `verification_allowed` sites (dense ~576,
  sparse ~611, tag ~661 and ~675). `tier_allowed` excludes `quarantine` **UNCONDITIONALLY** (not
  flag-gated): legacy/flag-off entries are `unclassified` (never quarantine), so this is behaviorally
  identical when the write-side flag is off, and it closes the Fable leak where action-flag-on but
  tiering-flag-off would inject quarantined text. When the flag is on, also prefer `core` for the
  injected `context_block`.
- **eviction tier-weight: DEFERRED** (research §Q8.2: extend `_eviction_score` only *after*
  classifier precision is measured; and it's inert unless `max_entries` is set). Not in Increment 1.
- **config.py**: `memory.tiering_enabled: False` in the `"memory"` block; `RetrievalConfig.from_mem_cfg`
  reads it (write-side gate only).
- **tests** (`tests/test_unit_memory_tiering.py`): each concrete rule maps to its expected tier;
  merge-branch raises-only; **flag-off ⇒ behaviorally identical** (same retrieval text, same mining
  dispositions, same eviction order) AND **id/dedup stable** (new fields absent from hash) — assert
  exactly this, NOT literal payload-byte identity (payload gains a `tier` key, which is benign);
  `quarantine` excluded from retrieval even with tiering flag off; `contradicted` never stored.

### Increment 2 — ActionMemory record + recurrence matcher (flag: `memory.memory_action_enabled=False`)
*(Fable-revised: signature is observable at the right lifecycle points — no reliance on a
truncation event that doesn't exist.)*
- **Two observability modes** (Fable Finding 1 — `retrieve_context_with_meta` runs at run START,
  before any error; there is NO per-attempt truncation `EventType`, only aggregate USAGE
  `max_token_retries`):
  - **Preconditions = pre-run predicates over (task text + COMPILED CONFIG STATE)**, checkable at
    run start. The Revenue-Ops fix is a *config* precondition ("role X in agency Y pinned at
    `max_tokens=768`"), detectable proactively from the compiled agency BEFORE the run — this is
    what lets a matched action be surfaced at run start.
  - **Mining a candidate** uses POST-run signals: terminal outcome `FAILED/ERROR` +
    USAGE `max_token_retries > 0` (+ the failed-required-delegation reason). No new event needed.
- **schema.py**: add optional `resolution: dict | None = None` (typed ActionMemory:
  `{problem_signature, scope, preconditions[], steps[{capability,args_template}], postconditions[],
  rollback, evidence, policy}`); persist; out of `id` hash.
- **new `src/fabri/memory/recurrence.py`** (pure functions, unit-tested, NO LLM):
  `canonicalize(signals) -> signature` (strip run-ids/timestamps/prices/paths; keep error class,
  phase, agency/role, provider/model family, retry behavior, finish reason, config values);
  `fingerprint(signature) -> str` (hash of stable fields); `applicable(action, current_state) ->
  bool` where `current_state` = compiled config + task (hard-field predicates must all match);
  `apply_confidence(...)` distinct from retrieval relevance.
- **pipeline.py miner**: on a mined recovery, write a **`quarantine`**-tier candidate ActionMemory
  (never `action`-tier at mine-time; promotion to `action` only after verification — Increment 3).
- **tests** (`tests/test_unit_recurrence.py`): the Revenue-Ops config signature matches a repeat
  with different session IDs; refuses a timeout / different agency / role already at 2048; a
  semantically-similar-but-inapplicable case recalls but fails the hard config preconditions.

### Increment 3 — Proposed-action surfacing + shadow executor + golden test (flag-gated, propose-only)
- **retrieval.py `retrieve_context_with_meta`**: at run START, evaluate matched action
  `preconditions` against current state (task + compiled config — the pre-run mode from Increment 2)
  and surface any *applicable* `resolution` into the returned `meta` (side-channel; `meta` consumers
  already use `.get()`, so this is additive-safe). Do NOT inject as prose. This is why the
  Revenue-Ops fix works at run start: its preconditions are config predicates, not runtime errors.
- **core/agent.py**: new prompt block (parallel to `FILE_EDIT_POLICY`, framed as firmly as
  `RETRIEVED_GUIDELINES_TASK_PRECEDENCE`) presenting the resolution as a **PROPOSED, unexecuted**
  action the model may choose to invoke via its normal tool call — no change to `_dispatch_tool_calls`.
  Start in **shadow** (propose + log, no auto-exec).
- **golden test** (`tests/test_memory_action_golden.py`): reproduce Revenue-Ops detection → scoped
  768→2048 proposal → (shadow) verify the proposal is correct → **refuse** on a near-but-inapplicable
  case. Uses the in-memory fake store pattern from `test_memory_evolution.py` + `ScriptedLLMBackend`.

## Gate (every increment)
`uv run --frozen pytest tests/ -q -k "memory or retriev or prun or pipeline or schema"` must stay
≥ **163 passed** (plus the new tests), and `uv run --frozen pytest tests/ -q --co` must stay 0
collection errors. **Flag-off ⇒ behaviorally identical** — same retrieval text, same mining
dispositions, same eviction order, and id/dedup stability — is a hard requirement asserted in tests
(NOT literal payload-byte identity: payloads gain benign `tier`/`resolution` keys). Note the `fabri`
CLI payload JSON dumps (`cli.py:353,479`) will show the new keys — that's expected and benign.

## Out of scope for Part 1 (deferred)
- Auto-execution beyond shadow/canary; MCP client OAuth/Streamable-HTTP upgrade (Part 2 connector
  work); the sales lead-gen company (Part 2, `fabri-rosters`); replacing `_eviction_score`.

## Landmines (verified)
- Never add `tier`/`resolution` to `MemoryEntry.id` hash (breaks idempotent upsert/dedup).
- `retrieval.py` line numbers shifted post-rebase; Codex must locate symbols, not trust numbers.
- Keep `--frozen` on all pytest runs (bare `uv run` rewrites uv.lock).
