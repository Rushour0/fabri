# Key decisions, honest results, and lessons

This is the decision log for the fabri project — every consequential call made between
2026-07-07 and 2026-07-24, why it was made, and what the evidence actually says. Read this
alongside `timeline.md` (what happened when) and `memory-system.md` (the deep dive on the
core bet). Sources: the session memory notes, benchmark result files under
`benchmarks/results/`, and the design notes under `docs/design/`.

---

## Part 1 — Strategic decisions

### 1. Two memory backends only: Qdrant + sqlite-vec (2026-07-07)

Weaviate was considered and rejected. The two existing backends already cover the axes that
matter — networked/production (Qdrant) vs embedded/zero-infra (sqlite-vec). Weaviate is a
lateral substitute for Qdrant, not a new capability axis, and its one real edge (native
BM25+vector hybrid fusion) became moot the same day when fabri's own FTS5 hybrid path was
fixed. The cheap future hedge, if ever needed, is a formal `MemoryStore` Protocol documenting
the 8-method store contract — not a third backend built speculatively.

**Principle:** fabri's differentiator is the self-improving memory loop, not the vector DB
underneath it.

### 2. Measure before tuning: observability + offline eval gate first (2026-07-07)

Retrieval quality was unmeasured, so the initiative deliberately landed observability (M3)
and a fast offline eval gate (M4, `src/fabri/benchmarks/retrieval_eval/`) *before* any
quality work. This paid off immediately — the eval exposed three real bugs (see Part 3).

**Principle:** any retrieval change must move an offline eval number before merging. Never
flip a default blind.

### 3. Counter paperclip-ai on the engine, not the control plane (2026-07-18/20)

paperclip-ai (open-source, tens of thousands of stars) is a control plane that schedules and
governs black-box external agents: org charts, budgets-as-caps, heartbeats, board-style
governance. The decision: do **not** compete on governance/org-chart parity, and do **not**
become a paperclip agent (that commoditizes fabri under their surface). Instead lead with
what they structurally cannot do:

- **Self-improving memory loop** — their agents are black boxes and cannot learn the
  company's work; fabri's can.
- **COGS-first economics** — paperclip *caps* spend; fabri *measures and prices* real
  per-run cost.
- **Open engine** — something you build products on, not just operate.

The Studio dashboard plan followed from this: slice 1 (durable sqlite RunStore, shipped
PR #48) and slice 2 (self-improving trend dashboard: cost-per-run and reuse-% over time) come
before any parity features (rolling budgets, scheduler/heartbeats, governance).

### 4. The memory-over-prompt pivot (2026-07-22)

The original memory model injected accumulated guideline text into prompts. The studies said
this was not producing wins (see Part 2). The pivot: a decision gate that tiers every mined
lesson into

- **prompt** — only high-severity, always-relevant rules (the existing capped path),
- **action** — retrievable memory applied as an automatic fix on recurrence,
- **drop** — low-value noise, discarded.

Two deliverables in strict order: (1) the memory-as-action engine redesign, proven on its own
benchmark first; (2) a sales lead-gen company in fabri-rosters as the real-world proving
ground. The central — still untested — bet: action-application beats text-injection *when a
relevant lesson exists*.

### 5. Convention capture must be engine-side, never prompt compliance (2026-07-23)

The AGENT_MEMORY saga (Part 3, item 6) falsified the idea that a model can be prompted into
reliably self-reporting its learned conventions. The ratified direction: a hybrid
deterministic gate with typed extraction and quarantine-by-default — the engine mines and
applies conventions; the model at most *selects a branch*, and even the effect-class label is
an engine constant, not a model-authored field (PRs #83–#89).

### 6. Model-role division of labor (2026-07-22, user-set)

Opus = manager/orchestrator, Sonnet = worker agents, Fable = plan critic, Codex exec =
implementation + research — with Claude verifying everything Codex claims. Most of the
engine work from 0.19 onward was built this way (research-first via Codex, adversarial
review, then flag-gated increments).

---

## Part 2 — The honest results ledger

This project's most valuable output may be its negative results. State them at real strength.

| Claim | Status | Evidence |
| --- | --- | --- |
| Cross-session guideline reuse fires | **Proven live** | dedup_key fix: reuse 0% → 100% from run 3 (session_delta); Wave-2 live crew run 3: reuse_rate 1.0, crew retrieved and re-fixed the seeded bug |
| Mine → store → retrieve → promote loop closes | **Proven live** | tactical→strategic promotion at the 3-session threshold, verified end-to-end (0.17.0 repo-agent session) |
| Memory improves task **quality** | **Not shown** | 36-arm re-measure (3 companies × 6 replicas, $4.82): no significant effect anywhere; the earlier "+100pp memory wins" AND the "memory hurts" headlines were both scoring artifacts |
| Memory makes runs **cheaper** | **Not shown** | cost mean Δ positive everywhere measured; on zero-failure tasks reuse just adds context tokens (cost +25% in the session_delta run). Do not claim "cheaper every run" |
| Benchmark has headroom (control can fail) | **Proven** | control failed the rewritten holdout in 4 consecutive smoke rounds |
| Passive mining can carry multi-branch protocols | **Falsified** | mining stores the *applied* branch verbatim, never the declared full protocol; first live proof 2026-07-22 |
| Prompted AGENT_MEMORY self-report works | **Falsified** | 0/4 emissions despite explicit instruction; the 0.19.3 hard-contract templates got 0/2 AND broke structured output (reverted in 0.19.4) |
| Convention supply pipeline works | **Proven (r7)** | mine → exact-hash-approve → transport → retrieve all worked; store held both branches including the needed one |
| Convention **application** works | **Open** | model still answered branch A citing branch-B evidence (rubric 0/4); suspected one-line wiring gap: `convention_mining_enabled` set only on training compiles, so the holdout-side branch-selection validator never arms |
| Retrieval config is a lever for memory wins | **Falsified** | config sweep (PR #68): not the lever; root cause was supply (~2 guidelines total, sub-agent traces never mined — fixed PR #70) |

**The through-line:** every "memory doesn't work" result decomposed into an engine or
instrument bug — supply (sub-agent traces unmined), dedup (paraphrase drift), truncation
(30-token cap), transport, evaluator artifacts. Each fix moved the frontier one layer deeper.
The current frontier is convention *application* at the holdout, which is suspected to be a
one-line wiring fix away from a real test of the core bet.

---

## Part 3 — Bugs worth studying (each one is a lesson)

1. **BM25 was a silent no-op** (`embedded_store.py::_fts5_query`): FTS5 treats space as
   implicit AND, so space-joined multi-word queries matched nothing and `hybrid` silently
   degraded to `dense`. Fix: OR-join + underscore split → hybrid recall@5 0.79 → 0.94.
   *Lesson: an unmeasured subsystem can be entirely dead and nothing tells you.*

2. **RRF k=60 is wrong for short two-pool fusion.** Web-scale k flattens `1/(k+rank)` so
   cross-pool agreement outranks the single best match. Retuned to 20 → recall@3 0.60 → 0.90.
   *Lesson: RRF k should be about pool size (10–20), not the literature default.*

3. **Guaranteed slots at the head steal rank 1.** Front-loaded `success_pattern` slots capped
   recall@1 at 0.13; moving them to reserved tail slots lifted recall@1 to 0.58 and MRR
   0.45 → 0.84. *Lesson: relevance owns the head; injection belongs at the tail.*

4. **Cached-token double billing (~11x on the cached portion).** OpenAI and Gemini report
   prompt tokens *inclusive* of cached tokens; Anthropic/Bedrock report them net. Pricing
   assumed net everywhere, so cached tokens billed at ~1.10x instead of 0.10x. Fixed PR #14;
   the invariant now lives in `pricing.py`'s docstring. *Lesson: normalize provider usage
   semantics at the backend boundary, and make tests assert exact dollar values.*

5. **Outcome classification lied about success.** A run with 13/13 failed tool calls could
   only ever report `SUCCESS_WITH_RECOVERY` (Slice A), and a child's recovery outcome was
   collapsed into `ok:true` by the parent (Slice B). Fixed with `Outcome.ALL_TOOLS_FAILED`
   (floor of ≥2 calls to preserve the legit single-failure-then-recovery case) and
   parent-side inspection of the nested outcome. *Lesson: trust the receipts, not the
   summary — a lesson the fixing workflow itself re-proved when its own success summary
   omitted a test-coverage gap.*

6. **The AGENT_MEMORY saga.** The self-report channel existed end-to-end
   (`memory/output.py` → `pipeline.py::extract_agent_memory`) but the model emitted it 0/4
   despite explicit instruction. Hardening it into a "hard output contract" (0.19.3) got 0/2
   *and* regressed structured output (the model embedded the block inside the JSON value;
   3/4 arms invalid, control included) — reverted within a day as 0.19.4. Later a live leak
   (`<!-- AGENTMEMORY -->` reaching users) showed emission is context-dependent, not
   format-dependent; PR #80 made it a real tag with leak-proof stripping. *Lesson: never
   build a load-bearing mechanism on model prompt compliance.*

7. **The paraphrase-drift dedup bug.** Guideline text is a fresh LLM paraphrase each run and
   entry IDs were `sha256(namespace::text)`, so cosine ~0.70–0.84 drift (below the 0.85
   threshold) meant every run inserted a *new* entry and promotion/reuse never fired. Fix: a
   deterministic `dedup_key` computed from stable inputs (kind + normalized task +
   tool/error/path), tried before cosine. *Lesson: dedup on stable identity, never on
   generated text.*

8. **The metric that couldn't see first reuse.** `from_prior_sessions` required
   hit_count ≥ 2, so the *first* cross-session reuse always read 0.0 (PR #79). Paired bug:
   `fabri studio --company` baked crew memory paths inside a mkdtemp tree, so the sharpest
   lessons died on restart. *Lesson: verify the metric can register the event you're trying
   to observe, before spending on the experiment.*

9. **stdlib shadowing with empty stdout.** Naming a module `tools/secrets.py` shadowed stdlib
   `secrets` for subprocess recipe tools (their own dir lands on `sys.path[0]`); litellm's
   import hit our file and the agent runner died with *empty stdout*. Renamed
   `secret_refs.py`. *Lesson: check `python -c "import <name>"` resolves to stdlib before
   naming modules in dirs that run scripts directly.*

10. **Evaluator artifacts flipped the study's sign — twice.** A fixed 60-char negation
    lookbehind produced false positives *and* false negatives; naive substring matching
    failed legitimate answers; "control" arms were still mining and retrieving within their
    own run, so 2/3 companies had zero usable controls. All fixed in 0.19.2; any past run can
    be re-scored without re-spending via `benchmarks/rescore_runs.py`. *Lesson: never trust a
    substring rubric; fix the instrument before believing any measurement in either
    direction.*

---

## Part 4 — Process lessons (feedback-grade, from the user)

- **Research the tool before building on it.** Read shipped docs and existing OSS
  implementations first; probing alone once shipped a false safety claim on
  grok-build-desktop. Probing finds what you thought to ask; docs tell you what to ask.
- **Never stash user WIP to attribute failures.** A `git stash pop` conflict (uv.lock
  regenerated mid-test) silently reverted three source files. Use a clean worktree or CI to
  attribute failures instead. Also: the local suite needs Qdrant —
  `docker compose up -d qdrant` — or ~84 tests fail with connection-refused, which looks like
  broken code but is environmental.
- **Gate the promo on the proof.** The user's standing rule through the smoke rounds: no
  success tweet, no rosters success update, until the thing demonstrably works ("if and only
  if it works"). Claims currently safe to make: reports real per-run COGS, learns from its
  own runs, open source. Not safe: "gets cheaper every run."
- **Secrets never go to the public repo.** `linkedin_redeem_check.applescript` (live gift
  tokens) stayed untracked by explicit decision, as did the blind-study label sheets
  (agreement-study integrity).

---

## Part 5 — Open questions (as of 2026-07-24)

1. **Does convention application close the loop?** Wire `convention_mining_enabled` on the
   holdout compile, add a deterministic extraction retry, run one ~$0.30 2-replica smoke
   before any 6-replica spend.
2. **Does action-application beat text-injection?** The 4-arm benchmark (corrected control /
   injection-as-today / tiered-injection / memory-as-action, plus a non-recurring holdout for
   false-apply rate) has not run yet.
3. **Does memory ever make runs cheaper?** Needs a failure-recovery task with real headroom,
   not a zero-failure task.
4. **The dedup-merge dilution issue** (fabri#78): merging keeps re-synthesized vaguer text
   over the sharp original — design needed.
5. **The 36-item agreement label sheet** (`benchmarks/agreement/label_sheet.md`) — human
   labels pending, needed to validate the evaluator.
