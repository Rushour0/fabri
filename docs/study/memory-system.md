# The memory system: fabri's core bet

This is a study document, not reference documentation. It exists to help you (Rushikesh) actually
understand what the last ~2 weeks of memory-system work did, in the order it happened, including the
dead ends. For the engineer-facing reference, see `docs/memory.md`. For the raw evidence, see
`BENCHMARKS.md` and `benchmarks/results/`. Everything below is traceable to a commit, a file, or a
memory note — nothing here is invented.

## Why memory is "the core bet"

fabri's pitch is self-improving agents: an agent that fails at something once should not fail at it
the same way twice, and lessons learned by one agent (or one session) should be available to the next
one. That only works if there is a working pipeline from "an agent did something, and it either failed
or succeeded" all the way to "a future agent, possibly a different agent, in a possibly different
session, gets the right piece of that experience back at the right time." Nearly every substantial
change to fabri in the last two weeks has been either building a piece of that pipeline, finding out a
piece of it was silently broken, or trying to prove the pipeline actually pays for itself. That's what
this document walks through.

The pipeline, at a glance:

```
run trace --> process_trace() --> compress_llm --> dedup_key merge --> promotion --> store
                                                                              |
                                                                              v
                                                              retrieval (dense+sparse fusion)
                                                                              |
                                                                              v
                                                          injected into next agent's system prompt
```

Every stage of that diagram has a real story: a bug that made it a no-op, a fix, and (usually) a
measurement that tried to tell whether the fix actually mattered end to end.

---

## 1. The retrieval stack: two backends, and a silent bug that erased half of it

fabri stores mined guidelines as vectors + text in one of two interchangeable backends, selected by
`memory.backend` in `runtime.build_memory_store` (`src/fabri/runtime.py:22-41`):

- **Qdrant** (`src/fabri/memory/store.py`) — networked, multi-process-safe, the production default.
- **sqlite-vec** (`src/fabri/memory/embedded_store.py`) — in-process, file-backed, no Docker
  required, meant for dev/CI/zero-infra use. It keeps a plain `guidelines` table, a `vec0` virtual
  table for ANN vector search, and an FTS5 virtual table for lexical (BM25) search.

**Decision (2026-07-07): no third backend.** A Weaviate backend was explicitly considered and
rejected — see the memory note `fabri-memory-backend-decision.md`. The reasoning: Weaviate is a
lateral substitute for Qdrant (same capability axis: networked vector DB), not a new axis like
"embedded, no infra" already covered by sqlite-vec. Its one real edge — native BM25+vector fusion —
was made moot the same day by fixing fabri's own hybrid path (see next section). The rule that came
out of this: only build a third backend on a concrete consumer pull, not speculatively. If backend
optionality becomes urgent, the cheap hedge is formalizing the existing 8-method store contract
(`upsert/query_by_vector/query_bm25/count/iterate/delete/get/find_similar`) as a real `MemoryStore`
Protocol so a new implementation is well-specified — that hasn't been done yet.

### The BM25/FTS5 no-op bug

fabri's default `retrieval_strategy` is `"hybrid"`: Reciprocal Rank Fusion (RRF) between a dense
(vector) result list and a sparse (BM25 lexical) result list. On the SQLite backend, the BM25 side
uses FTS5's `bm25()` ranking function. `_fts5_query` builds the FTS5 query string by tokenizing the
input and joining the tokens together — and the original implementation **space-joined** them. FTS5
treats a bare space between terms as an implicit `AND`, so any multi-word query effectively required
every single token to be present in a guideline's text to match at all. In practice this meant lexical
search matched almost nothing, and "hybrid" silently degraded into "dense-only" every time.

This was found via the offline retrieval eval (`python -m fabri.benchmarks.retrieval_eval`, built as
part of the M3/M4 observability initiative — see `docs/design/memory-observability-plan.md`), which
gave the team a number to look at instead of vibes. The fix: OR-join the tokens instead of
space-joining them (`src/fabri/memory/embedded_store.py`, function `_fts5_query`). Effect: hybrid
recall@5 went from **0.79 to 0.94** on the offline eval fixture. There's a regression test guarding
this specific failure mode, `test_hybrid_bm25_is_alive`.

Two more retrieval-quality bugs were found and fixed the same day, once the eval could actually
discriminate strategies apart from each other:

- **RRF `k` was 60, the web-scale default, and it was wrong for fabri's short pools.** fabri fuses two
  short candidate lists (`~2*top_k` each), and at `k=60` the term `1/(k+rank)` is so flat that mere
  *agreement* between the dense and sparse lists could outrank the single best match from either list.
  Retuned to `k=20` (now the `memory.rrf_k` config knob). Effect: hybrid recall@3 went from 0.60 to
  0.90.
- **Guaranteed `success_pattern` slots were front-loaded into ranks 1–2**, which meant a
  guaranteed-but-less-relevant entry could bump the single most relevant guideline out of rank 1. Moved
  to reserved **tail** slots instead — relevance always owns rank 1. Effect: recall@1 went from 0.13 to
  0.58, and MRR from 0.45 to 0.84, and this helped the dense-only strategy identically (front-loading
  was hurting everyone, not just hybrid).

The rule that came out of all three: **any retrieval change must move an offline eval number before it
merges; never flip a retrieval default on vibes.** This eval gate (`M4` in the observability plan) now
exists specifically to enforce that. Full mechanics — RRF math, MMR diversification, temporal decay,
domain routing, the tag-hit floor, the tail-slot reservation math — are documented in `docs/memory.md`
§2 if you need the exact formulas; this document only covers the "why" and "what changed."

### Injection caps: two different limits, and a subtle interaction

Retrieved guidelines are wrapped in a `<retrieved_guidelines>` block in the agent's system prompt,
explicitly marked as reference-only, never an instruction. Two separate caps bound how much text gets
in:

1. `guideline_max_tokens` (default 30) — caps how long a guideline's *text* is when it's synthesized
   by the compress LLM at mining time (`src/fabri/memory/compress.py`).
2. `MAX_INJECTED_GUIDELINE_CHARS` (500, hardcoded) — caps how long the text can be at *injection* time,
   truncated at a word boundary (`src/fabri/orchestrator/retrieval.py`).

These look redundant but they're not: a 2026-07-22 "headroom smoke" test (see §4 below) found that the
**30-token mining-side cap is almost always the binding constraint**, not the 500-char injection cap —
a synthesized guideline gets truncated down to something like "Action: Map MITIGATED. Expected
outc..." before it ever reaches the injection stage. This turned out to matter a lot later (see the
convention-mining section).

---

## 2. Guideline mining: turning a trace into a lesson

`process_trace()` (`src/fabri/orchestrator/pipeline.py`) walks a session's trace after a run and mines
three kinds of events: tool calls that errored, "discrepancy" events, and the final outcome. A
dedicated **compress LLM** — deliberately built with an empty tool list, separate from the agent's main
tool-schema-laden LLM — synthesizes each candidate into a short, four-clause guideline (Trigger,
Evidence, Action, Expected outcome) via `synthesize_guideline()` / `synthesize_success_pattern()` in
`src/fabri/memory/compress.py`.

### Bug #1: sub-agent traces were never mined at all (the "~2 guideline ceiling")

This is probably the single most consequential bug found in the whole initiative. `process_trace()`
was only ever called from the top-level `fabri run` CLI path (`cli.py`). But fabri companies are
built out of a manager agent that delegates to specialist sub-agents via `spawn_subagent`
(`src/fabri/tools/agent_runner_tool.py`) — and **that path never called `process_trace()` on the
child's own trace**. So in any multi-agent company, only the manager's thin trace ever got mined, and
every specialist's actual work — the traces where interesting failures and lessons would actually live
— was silently discarded.

This was the root cause behind a benchmark story that had been circulating for a while: "memory
doesn't beat a frozen control, and tuning retrieval config doesn't move the needle." Turns out that
story was upstream-poisoned: with sub-agent traces discarded, the memory store a company actually had
to draw on was capped at roughly 2 guidelines total, no matter how much retrieval tuning happened
downstream. Retrieval-config tuning on a nearly-empty store is a no-op by construction.

**Fixed in PR #70** (commit `1203c94`, 2026-07-21): `agent_runner_tool.py` now calls
`process_trace()` on each sub-agent's own trace after it finishes, mirroring what `cli.py` already did
for the top-level run. Mining is gated by `memory.mining_enabled` (default True) AND the env var
`FABRI_DISABLE_SUBAGENT_MINING` is NOT set (an experiment-only escape hatch used to build a
mining-off benchmark arm). Any exception during mining is swallowed and logged to stderr — a mining
bug can never flip the sub-agent tool's own success/exit code. Gate at merge: 1173 tests passed, +16
new tests.

The live guideline-supply experiment that would prove the now-higher supply actually makes memory
*win* has not been run — it's spend-gated (~$2.85, and only an OpenAI key was available in that
session, not Anthropic/Bedrock). This is called out explicitly as an open item in `BENCHMARKS.md`'s
"honest gaps" section — worth knowing this fix is proven to increase supply, but not yet proven to
move the win/loss needle.

### Bug #2: cross-session reuse was structurally stuck at 0%

Separately (2026-07-19, before the sub-agent fix), a metric called `guideline_reuse_rate`
(`from_prior_sessions / retrieved`, `src/fabri/core/agent.py`) was found to be permanently 0%.

Root cause: every time a guideline is mined, its text is a **fresh LLM paraphrase** — the compress LLM
doesn't reproduce the same wording run to run. But `MemoryEntry.id` was `sha256(namespace::text)`, so
paraphrase drift alone (MiniLM cosine similarity around 0.70–0.84 between two paraphrases of "the same"
lesson, below the 0.85 merge threshold) was enough to make `find_similar()` miss the existing entry and
insert a brand-new one every single run. `hit_count` and `session_ids` on any given lesson never grew
past 1, so promotion (tactical → strategic) and reuse never fired — structurally, not by bad luck. The
merge logic itself (`memory/pruning.py`) was always correct; it just never got a chance to run.

**The fix**: add a deterministic `dedup_key` to `MemoryEntry`, built from *stable* inputs instead of
the paraphrased text — kind plus normalized task text plus kind-specific fields (failed tool name +
first line of the error for `tactical`, task + tool names for `success_pattern`, file path for
`discrepancy`). `ingest_guideline()` now looks up an existing entry by `dedup_key` first, and only
falls back to the cosine-similarity path if there's no exact key match. Landed in commit `87219d9`
("fix(memory): deterministic dedup_key so cross-session guideline reuse fires", PR #42).

**Live result** (session_delta benchmark, gpt-4o-mini, 6 runs): reuse went **0% → 100%** starting at
run 3 — the same failure recurred, matched by dedup_key, got promoted to strategic, and was reused.
The mechanism is proven. But the same experiment found reuse *increased* cost by 25% with step count
flat, because the test task had zero failures to avoid — reusing a guideline just adds context tokens
with nothing to save. **"Gets cheaper every run" was explicitly flagged as NOT yet demonstrated** —
only "learns and reuses cross-session" was. The `fabri[self-improving]` extra's marketing copy
(PR #41) originally claimed "cheaper every run"; the recommendation on record was to soften that to
"learns/improves" until a failure-recovery benchmark (fail once, recover, get cheaper on repeat)
actually backs the cost claim. That benchmark has not been completed as of this writing.

---

## 3. Proving (or failing to prove) that any of this helps

Having a working pipeline and having a pipeline that *pays for itself* are different claims, and fabri
has been careful — sometimes painfully so — to keep those separate. The honest history:

- **Pilot memory-vs-control study** (`benchmarks/README.md`, `47c86c6`): reversed at 10 replicas —
  an early positive signal did not hold up under more data.
- **Reliability Labs company** (`cea1311`): no benefit found, second company tested.
- **2026-07-22 evaluator bug**: the study comparing memory to a frozen control had its own scoring
  bugs. Forbidden-term detection used a fixed 60-char negation lookbehind (false positives *and* false
  negatives — a negation in an earlier sentence could wrongly exempt a real policy violation).
  Required-term detection used naive substring matching, so paraphrases like "share further
  customer-facing updates" failed to match a required phrase "further update". And "control" arms were
  still mining and retrieving within their own run in two of three companies, so those controls
  weren't actually memory-free. All three fixed same day (sentence-scoped negation, order-preserving
  proximity matching, and real `mining_enabled`/`retrieval_enabled` off-switches with a verified
  0-guidelines-retrieved control). Re-measured: 36 arms, 3 companies × 6 replicas, $4.82 spent —
  **no significant memory effect on quality or cost anywhere.** The explicit lesson recorded from this:
  "never trust a substring rubric" — both the earlier "memory hurts" headline and an initial "+100pp
  memory wins" read were scoring artifacts, not real effects. Shipped as v0.19.2 (PR #73).

The conclusion drawn from all of this (documented in the memory note
`fabri-memory-over-prompt-pivot.md`): text-injection of accumulated guidelines, as the *only*
mechanism, was not moving the needle. Two candidate causes were named — low-value lessons crowding
already-capped retrieval slots, and train/holdout scenarios often having nothing in common to reuse —
but neither was proven; the report calls crowding a "leading hypothesis," not a finding. This is what
motivated the pivot described next.

---

## 4. The pivot: memory-over-prompt (2026-07-22)

The decision, in one sentence: stop treating "inject more accumulated guideline text into the prompt"
as the only lever, and build a second channel where a recognized recurring problem gets **fixed by the
engine**, not just described to the model.

The new mental model tiers every mined lesson into one of:

- **prompt** — high-severity, always-relevant rules (today's capped fenced-text path, unchanged).
- **action** — retrievable memory applied as a fix on recurrence; the *engine* auto-applies it,
  the agent doesn't have to notice and comply with prose.
- **drop** — low-value or noisy lessons, discarded.

The central bet stated explicitly in the pivot note is still **untested as a controlled comparison**:
"action-application beats text-injection when a relevant lesson exists." The plan calls for a 4-arm
benchmark (no-memory control / today's injection / tiered-injection / memory-as-action) to actually
settle this; that full 4-arm run has not happened yet — what has happened is building the ActionMemory
mechanism itself and testing it live on one recovered incident (next section).

This pivot also explicitly separates two deliverables that must not be merged together: (1) the
memory-as-action engine redesign, proven on fabri's own engine benchmarks first, and (2) a sales
lead-gen company built in the separate `fabri-rosters` catalog repo as the real-world proving ground
for whatever the redesign produces. The lead-gen company work is out of scope for this document.

---

## 5. ActionMemory: memory that writes config, not just prose

Shipped in commit `6c397fd` ("feat(memory): executable ActionMemory with a fail-closed capability
allowlist"), motivated by a live Revenue Ops smoke test where an agent kept hitting 128-token
truncation failures — the fix was mechanical (raise `max_tokens` for the affected roles) and didn't
need creative judgment, which made it a good first case for "let the engine just do the fix."

### How it's gated

Two independent config switches, **both default `False`**:

- `memory.memory_action_enabled` — turns on surfacing applicable remembered actions as shadow
  proposals (detected, logged, not applied).
- `memory.memory_action_apply_enabled` — the separate, explicit opt-in to actually *write* the change.
  Requires the first flag to have produced proposals; leaving this `False` keeps the system
  shadow-only / log-only even with detection on.

### The allowlist: exactly one capability, heavily fenced

The only executable capability today is `configure_role`, with args `{role, max_tokens}`. Validation
(`src/fabri/orchestrator/action_execution.py`, function `_plan_resolution`) is fail-closed and rejects:
non-idempotent/multi-attempt policies, a retry cap that isn't strictly greater than the current cap, a
retry cap more than 2x the current cap, any retry cap over 32768, roles not present in the trusted
manager's own config, and any role whose on-disk config no longer matches the stored precondition
(someone changed it since the lesson was recorded). Applied writes are atomic — tempfile + `Path.replace`
— with automatic rollback of every already-written file in the batch if a later write fails.

### How a candidate gets mined and matched

`build_truncation_action_candidate()` (`src/fabri/memory/action_mining.py`) mines a deterministic
cap-increase candidate whenever `observed_max_token_retries > 0`, regardless of whether the run
ultimately succeeded — a recovered run still proves the configured cap was too low. Mined candidates
start at `tier="quarantine"`, `policy.approval="shadow"` — unverified and excluded from normal
retrieval by construction until something verifies them.

Matching a *new* failure against a stored ActionMemory is deliberately narrow and deterministic:
`recurrence.py`'s `canonicalize()` extracts only 9 whitelisted fields (phase, agency, roles,
error_class, cause, configured_cap, retry_cap, model_family, finish_reason), hashes them in fixed
order, and `applicable()` requires exact scope + role + cap match plus every equality precondition —
no fuzzy matching. `apply_confidence()` scores 0.90–1.00 for an exact match, 0.45–0.60 for
applicable-but-not-exact, and ≤0.10 otherwise.

### Honest status

Live-demonstrated exactly once (the Revenue Ops truncation recovery). **Not proven to beat a frozen
control** — both gates are off by default, and the memory-vs-control benchmarks run so far (§3) show
no measured benefit on the companies tested. Offline unit-proven (the candidate-satisfies-matcher
logic has tests), not yet live-functional for the harder case — cross-agent config where the
sub-agent process can't see sibling-role or company-level config, which pushed the real detection logic
up to the orchestration/compile layer in a later increment.

---

## 6. Tiering and the quarantine gate

`MemoryEntry.tier` (`core | retrieve | action | quarantine | unclassified`) was added behind
`memory.tiering_enabled` (default `False`). `classify_tier()` (`src/fabri/memory/pruning.py`) is a
small deterministic function: contradicted entries always go to `quarantine`; verified strategic
entries with 2+ distinct source sessions go to `core`; unverified, generic, single-session
success-pattern entries go to `quarantine`; everything else defaults to `retrieve`.

The one rule worth internalizing: **quarantine exclusion from retrieval is unconditional.** Even with
`tiering_enabled=False`, `entry_allowed()` still rejects `tier == "quarantine"` at all four
candidate-fetch sites (dense, sparse, tag-hits, and the optional global tier). Only the "prefer core
first" re-ranking behavior is actually gated behind the `tiering_enabled` flag. This means quarantine
was designed from day one as fabri's hard safety backstop, not an optional feature — a bug in the
tier classifier can make something wrongly *not* quarantined, but a quarantined entry cannot leak into
context regardless of any other setting. This backstop is what convention mining (§7) leans on hardest.

---

## 7. Convention mining v1: declared multi-branch protocols

This is the newest and most structurally interesting piece, and it exists because of a specific,
well-diagnosed failure mode found during the 2026-07-22/23/24 "headroom smoke" tests.

### The finding that motivated it

Three 2-replica smokes of a `support_hq_safe_incident_response` scenario found something important:
once a rewritten holdout dataset was actually hard enough that a memory-free control failed 6/6, memory
still failed **every arm too** — but for three separate, individually verified engine reasons, not one:

1. An over-strict transport check aborted runs unnecessarily (fixed, commit `cfb2f15`).
2. The 30-token `guideline_max_tokens` cap (§1) physically truncated protocol lessons mid-sentence —
   "Action: Map MITIGATED. Expected outc..." — before they could carry a full multi-branch protocol.
3. **The real, structural finding**: even at a higher token cap, mining only ever stores the branch
   that was *actually applied* during the training run (call it branch A), never the *declared* full
   protocol with all its branches (A, B, C...). So a holdout case that needed branch B got nothing
   useful, even though the agent had, at some point, been told the full multi-branch rule.

Separately, the `<!-- AGENT_MEMORY -->` self-report channel — where the model itself was supposed to
emit a structured summary block at the end of a run — existed end-to-end in the code
(`memory/output.py` → `pipeline.py::extract_agent_memory`) but the model **emitted it in 0 of 4
training runs** despite an explicit prompt instruction. This is the smoking gun for why passive,
prompt-compliance-based capture doesn't work: you cannot rely on the model to reliably self-report a
structured artifact just because you asked it to in the prompt.

**The conclusion, stated plainly in the memory note**: "convention capture must be engine-side, never
model prompt compliance."

### The AGENT_MEMORY tag saga (a real, documented failure)

Before landing on the engine-side approach, one more attempt was made to force reliable self-reporting
by making the output format a *hard contract*:

- **0.19.3** (commit `4ab8cb1`): made the `AGENT_MEMORY` block a hard output contract on every agent
  response.
- This made things *worse*, not better: smoke #4 showed 0/2 emission in training (no improvement) AND
  a new regression — the model started embedding the block *inside the JSON response value* for
  structured-output agents, breaking 3 of 4 arms (including the control arm) with `invalid_output`
  errors.
- **Reverted the same cycle, shipped as 0.19.4** (commit `6a3d0a0`, reverting `482e9aa`, PR #75).

This is worth remembering as a general lesson, not just a memory-system footnote: making an LLM output
format a "hard contract" via prompt instruction alone doesn't reliably work, and can actively break
things that were working before, in ways that are easy to miss if you're not running structured-output
regression checks.

The eventual, correct fix was different in kind: **PR #80** (commit `525e8a8`) made `AGENT_MEMORY` a
real `<AGENT_MEMORY>...</AGENT_MEMORY>` XML-style tag with regex-tolerant leak-proof stripping — a
real leaked variant, `<!-- AGENTMEMORY -->`, had actually reached live users before this fix, which is
why the stripping needed to be tolerant of format drift, not just an exact string match. This didn't
fix the *emission* problem (the model still didn't reliably emit the block — see the round-5 smoke
below), but it fixed the *leak* problem: if the model does emit something block-shaped, it reliably
gets stripped from what the user sees instead of leaking raw markup.

### What convention mining v1 actually does

Convention mining v1 (`d9444a7`, PR #83, merged as part of the **v0.20.0** release, 2026-07-24) is the
engine-side answer. Instead of relying on the model to self-report, or on passive trace-mining to
somehow capture a "declared" branch it never applied, this adds a dedicated extraction pass whose whole
job is to capture the full protocol.

Key files: `src/fabri/memory/conventions.py` (the `ConventionRecord` type + rendering + budget
enforcement), `src/fabri/memory/convention_extraction.py` (the typed extraction pass hooked into the
mining pipeline), `src/fabri/orchestrator/convention_application.py` (retrieval-time branch selection
and copy-in).

It is built directly on top of the five decisions ratified by the operator on 2026-07-24 in
`docs/design/convention-mining-decisions-proposed-2026-07-22.md` — worth reading in full if you want
the reasoning behind each number, but the shape is:

1. **Who can create durable policy?** Trusted, operator-selected company/agency config is sufficient
   authority for its own scope (`verification="config_verified"`). Anything mined from task text, tool
   output, or model output is *always* stored `verification="unverified", tier="quarantine"` — even
   after a successful or rubric-verified run — until a human adds its exact
   `(scope, key, version, branch_mapping_hash)` to `memory.convention_approvals`. Recurrence, hit
   count, similarity, and run outcome are explicitly *never* sufficient to promote a task-mined
   convention out of quarantine. Any change to the branch mapping produces a new hash and needs a new
   approval.
2. **Which effect classes are allowed?** Only `effect_class="response_mapping"` in v1 —
   structured-field mappings, no tool calls, role delegation, config paths, credentials, file
   mutations, network actions, or approval bypasses. Non-executable workflow conventions ("escalate to
   legal") are explicitly out of scope for now; an otherwise-valid convention with a disallowed effect
   stays quarantined with reason `effect_class_not_allowed`, it's never silently rewritten into
   something that fits.
3. **Ambiguous branch selection**: preserve natural-language conditions (don't force a formal predicate
   grammar yet), but fail closed — the model must return exactly one `selected_branch_id`, a
   deterministic validator checks it against the mapped fields, one corrective retry is allowed, and
   if it still can't resolve to exactly one branch the engine applies no convention fields and asks for
   task-local clarification (or returns `convention_not_applicable`).
4. **Budget/lifecycle defaults**: 384-token render budget (overflow quarantines the whole candidate,
   never truncates it — this is the explicit fix for the 30-token truncation problem that started this
   whole investigation), 2–8 branches, 256-entry per-scope quota, 180-day TTL, and supersession
   restricted to the same authenticated source or a human.
5. **What unlocks `core` (always-on) placement?** Authorized declaration is enough for
   `tier="retrieve"`, but never for `tier="core"`. Core requires 5+ distinct future-task sessions,
   every branch independently exercised and scored, a pre-registered 30-case offline comparison showing
   a 5-point task-success gain, and a 100-use live canary with zero safety regressions, then an explicit
   human tier change. This is flagged by the ratifying decision itself as the least-confident of the
   five — the numeric thresholds are conservative placeholders, not derived from real convention
   telemetry.

### The follow-up fix cycle (this is where it got interesting)

Convention mining v1 did not work cleanly on the first landing — which is honestly useful to know,
because the fix sequence shows what "fail-closed" costs in practice. All landed same-day, 2026-07-24:

- **PR #84** (`b172a08`): a smoke run aborted its memory arms entirely. An *absent*
  `convention_allowed_effect_classes` key (true for any raw compiled config that never went through
  full default-merging — exactly what the benchmark harness reads) was defaulting to an *empty list*,
  rejecting every candidate as `effect_class_not_allowed`. Fixed so absent-key means the ratified
  default, explicit-empty-list still means operator lockdown. Also: the study harness was approving
  every quarantined candidate and treating any failed approval as a hard failure — it now pre-filters
  through the same admissibility gate an operator would use and only fails if *zero* approvals promote.
- **`c6cfac6`**: added the `memory.profile: evolving` one-line convenience (bundles
  `guideline_max_tokens: 120`, tiering, convention mining, postmortems, evidence-gated success
  patterns, ActionMemory propose-only — deliberately never touches approvals, trusted sources, core
  placement, or action-apply, since those are human-authority gates a profile can't silently grant).
  Also hardened extraction to capture declared protocols even when only one branch was exercised in
  training — the direct fix for finding #3 above.
- **PR #87** (`e1ce949`): fixed convention *placement* (company-scope conventions weren't reaching
  every store) and branch selection's marked-line channel.
- **PR #88** (`90e6626`): moved convention *application* fully engine-side — "the engine performs the
  convention copy; the model only selects the branch." A real design tightening: the model's only job
  is picking which branch applies (its actual strength), while copying mapped field values into the
  response became deterministic engine code, removing a whole class of "model paraphrased the value"
  risk.
- **`1a9a121`** (rounds 8–10 of live smoke): models were free-texting the `effect_class` field no
  matter what schema said ("customer-evidence response mapping" instead of the literal
  `response_mapping`). Since v1 only allows one effect class anyway, the fix removed the model's
  ability to author that field at all — extraction drops any model-emitted value and the engine
  hardcodes it. Branch *content* is still independently rejected by the quarantine-reason check; this
  closed a formatting failure mode, not a safety check. Gate: 1448 passed, 1 skipped.

### Current state (as of 2026-07-24, end of session)

Per the memory note `fabri-headroom-smoke-findings.md`, rounds 6–7 of live smoke testing found:
**supply is now solved — the mine → approve → transport → retrieve pipeline all worked**, with a
holdout replica's store correctly holding both declared branches including the one it actually needed.
But the model still answered with the wrong branch, citing evidence for the *other* branch, and scored
0/4 on the rubric. The suspected last domino, not yet fixed at session end: the benchmark harness only
enabled `convention_mining_enabled` on the *training* compile step, so the holdout agent's branch-
selection validator was never armed on the holdout side — a one-line wiring gap, not a design flaw.
Also noted: extraction is stochastic (one replica mined zero conventions on a given run), so a
deterministic-extraction retry is still on the list.

**The honest summary**: convention mining v1 is real, shipped, gated behind five default-off flags,
CI-green, and has closed the supply-side gap it was built to close (the store now genuinely holds the
full declared protocol, not just the applied branch). It has **not yet been shown to fix the
downstream benchmark** — the model's branch-selection behavior at inference time is the next thing to
debug, and the user's explicit gate ("no success tweet, no rosters-page success update until it
actually works") was still in force at the end of the last session.

---

## 8. What's proven vs. what's still an open bet

Worth keeping these two lists mentally separate — it's easy to conflate "we built the mechanism" with
"we proved the mechanism helps," and this initiative's whole arc is a lesson in why that conflation is
dangerous.

**Proven (verified against source, or demonstrated live):**
- Hybrid retrieval was silently broken (BM25 no-op) and is now fixed and eval-gated.
- RRF fusion constant and success-pattern slot placement were measurably wrong and are now fixed.
- Sub-agent traces were never mined (the "~2 guideline ceiling") and this is fixed — supply is
  measurably higher now (PR #70).
- Cross-session guideline reuse was structurally impossible (paraphrase drift breaking dedup) and is
  now fixed — reuse fires 0% → 100% live on a repeated-failure task.
- ActionMemory's fail-closed capability allowlist is real, tested, and has recovered one real live
  incident (Revenue Ops truncation).
- Quarantine's exclusion from retrieval is an unconditional backstop, verified in code at all four
  candidate-fetch sites.
- Convention mining v1's supply side (store the full declared protocol, not just the applied branch)
  is now working, verified in the round-7 smoke.
- The AGENT_MEMORY hard-contract approach was tried, found to actively regress structured output, and
  reverted — a real, documented negative result.

**Still open / unproven:**
- Memory-vs-frozen-control has shown **no measured benefit** on every company tested so far, across
  multiple evaluator-bug fix cycles — this is the headline finding a reader should not lose sight of.
- "Gets cheaper every run" is not demonstrated; only "learns and reuses cross-session" is. The
  failure-recovery benchmark that would demonstrate a real cost drop has not been completed.
- ActionMemory's benefit versus a frozen control is unproven; both its gates default off in production.
- The core 4-arm memory-as-action-vs-injection benchmark that the whole pivot was built to justify has
  not been run.
- Convention mining v1's actual downstream effect (does the agent pick the right branch at inference
  time, using the now-correctly-supplied convention) is still failing as of the last recorded smoke,
  pending a one-line holdout-compile wiring fix and a deterministic-extraction retry.
- Core (`tier="core"`, always-on) convention placement has never been exercised — its thresholds are
  explicitly flagged as the least-confident numbers in the whole ratified decision set.

If you want the single sentence version of where this stands: **the plumbing for self-improving memory
is now largely correct and individually verified, piece by piece, but the end-to-end claim that fabri
"gets better every run" has not yet been demonstrated on a real benchmark — every attempt so far has
either found no effect or found the effect was a measurement bug.** That is not a failure of the
project; it is what honest, eval-gated development of a hard claim looks like, and the next session's
job is closing the last domino on convention mining and then finally running the 4-arm comparison the
pivot was designed around.
