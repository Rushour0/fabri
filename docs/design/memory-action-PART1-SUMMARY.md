# Part 1 — Memory-as-Action engine redesign — summary & handoff

**Branch:** `agent/memory-action-research` (worktree), off PR #71 tip `8743fc8`. Rebase onto `main`
after #71 merges. **Nothing here touches PR #71's `agent/memory-evolution` branch.**

## What shipped (5 commits, all flag-off by default, offline test-green)

| Commit | Increment | What |
|---|---|---|
| `3ae8955` | 1 | `MemoryEntry.tier` (core/retrieve/action/quarantine) + deterministic classifier (concrete rules, new-entry + merge raise-only) + **unconditional** quarantine retrieval exclusion via `entry_allowed` at all 4 filter sites + `memory.tiering_enabled` flag |
| `47e823d` | 2 | `MemoryEntry.resolution` typed ActionMemory field + pure `memory/recurrence.py` (canonicalize → fingerprint → applicable → apply_confidence; no LLM/IO) |
| `306d4ba` | 3 | shadow proposed-action surfacing (`retrieval.propose_actions` → `meta` → firm prompt block) + `memory.memory_action_enabled` flag + Revenue-Ops golden test |
| `0cd404f` | review | bounded + fail-closed `propose_actions`; honest boundary test + source comment for the live gap |
| `0fdc645` | 4 | `orchestrator/action_detection.py` — orchestration-level detection (rich `current_state` from a manager's child configs) that resolves the review's critical gap |

**Gate:** area suite (`-k "memory or retriev or prun or pipeline or schema or agent"`) = 244 passed;
whole-suite collection = 1198, 0 errors; 6 new test files, ~30 new tests. Run offline with
`uv run --frozen pytest …`. **Both feature flags default OFF; flag-off is behaviorally identical**
(same retrieval text / mining dispositions / eviction order / system prompt / id / dedup) — asserted
in tests. Payload JSON gains benign `tier`/`resolution` keys (documented, not behavior).

## Design (grounded in `docs/research/memory-tiering-patterns.md`, citations spot-verified)
Tiering sits **on top of** the existing `verification` filter (trust) and **upstream of**
`pruning.py` eviction (unchanged). ActionMemory = typed `{problem_signature, scope, preconditions,
steps[capability→args], postconditions, rollback, evidence, policy}`; recurrence = canonicalize →
exact fingerprint → hard-field match → embedding recall → applicability predicate. Rollout is
**shadow → canary → auto** (only shadow is built). Q4 research finding: **no controlled evidence**
that tiering beats full-context injection for work agents → ship behind a flag and MEASURE; no
quality-win claim.

## Honest status — what is and isn't done
- **DONE (mechanism, offline-proven):** tiering (live via retrieval), recurrence matcher, shadow
  surfacing at both single-agent and orchestration layers. All unit-tested, review-hardened.
- **NOT done (needs decisions/spend — parked in `memory-action-OPEN-QUESTIONS.md`):**
  1. **Live proof of the cost/retry win** — needs live OpenAI runs vs `fabri-rosters` fixtures
     (spend-gated; frozen 7-company shared agency must not be mutated — only an ephemeral copy).
  2. **Production wiring** — orchestration detection is not called from `cli.py::cmd_run` yet.
     Assumed rollout: benchmark-apply + production **shadow** (log only), not auto-apply.
  3. **The miner (Increment 2 deferred)** — nothing lands in the store as an ActionMemory until a
     miner writes quarantine-tier candidates from real recovery traces; that touches PR #71's
     `pipeline.py` mining path, so it was left out.
  4. **Scope identifiers** — `company`/`agency` aren't persisted in compiled YAML; a real hook must
     derive them (`derive_scope_from_collection`) or thread them through compile time.

## Recommended next steps (in order)
1. **Benchmark proof (needs spend OK):** seed a Revenue-Ops ActionMemory, apply it to an ephemeral
   compiled copy (mirror `company_memory_study.py::apply_retrieval_overrides`), run the 4-arm study
   (no-mem control / injection-today / tiered / memory-as-action) on a recurrence-shaped holdout,
   measure cost + retries. This is the Part-1 payoff the whole redesign is a bet on.
2. **Production shadow wiring:** call `action_detection.detect_proposed_actions` from `cmd_run` /
   manager runners (detect + log, no apply).
3. **The miner:** write quarantine-tier ActionMemory candidates from recovery traces (coordinate
   with PR #71's mining path).
4. **Part 2:** the sales lead-gen company in `fabri-rosters` as the real-world proving ground.
