# Handoff — memory benchmark instrument repair (2026-07-22)

**Status: the instrument is repaired and trustworthy. The experiment has not been run.**
No paid run was spent in this session. Nothing is pushed. Two commits sit on
`feat/action-memory-and-benchmark-instrument`.

---

## 1. Why this work happened

`benchmarks.json` publishes per-company memory-vs-control records to fabri.rushour0.com.
Two problems made those numbers unusable as evidence:

1. **The scorer had no validity evidence.** 57 unit tests proved `score_text` did what it was
   told; nothing proved what it was told was right. Two bugs had already been found by accident.
2. **The benchmark was built so memory could not help.** Every holdout prompt restated its own
   rubric, so a no-memory control had everything it needed from the prompt. Two of three
   companies sat at 100%, p = 1.0 everywhere. No headroom means no experiment.

## 2. What was found

An audit of `score_text` against all 118 archived agent outputs found **four more defects**, and
the publication path had **two worse ones**. Every finding below was reproduced by hand.

### Scorer defects (fixed)

| defect | direction | evidence |
|---|---|---|
| a required term satisfied by its own **denial** ("rollback completion is *not confirmed*") | **inflates** | 2 real archived runs passed while denying the required fact |
| "**not only** … the fix was deployed" suppressed a genuine forbidden hit | **inflates** | reproduced directly |
| `blame` matched inside `blameless` | deflates | reproduced directly |
| `verify` did not satisfy `verification` | deflates | 1 real archived run |

The two false *passes* matter most: no prior audit had looked for the inflating direction.

### Publication defects (fixed)

1. **`rescore_runs.py` graded a multi-company run root against ONE company's rubric.**
   `benchmarks/runs/full-evolution-20260721/` holds three `case_id`s; Reliability Labs and
   Revenue Ops arms were being scored on Support HQ's rubric. The command in the tool's own
   docstring did this.
2. **Percentages printed beside a denominator that was not theirs.** Revenue Ops showed
   `n=10 … corrected=100.0%`; the real basis was **1 scorable arm**. `build_record` had the same
   defect, and that is what feeds the public page.

### The near-miss worth remembering

The headroom rewrite replaced every `expected.forbidden` list. Because archive re-scoring reads
the *current* dataset, regenerating the published records produced a **reversal** — revenue-ops
memory 100%→75%, control 66.7%→100%. That was not a correction; it was old runs graded against a
rubric that did not exist when they ran. Publishing it as "corrected" would have been fabricating
a result.

Fixed via per-case `legacy_expected`, so a run is always scored by the rubric of its own date, and
records now carry rubric provenance alongside scoring mode.

### The punchline

With era-correct rubrics, **all three published 2026-07-22 records regenerate byte-identical.**
Every published percentage has a full denominator (5/5, 6/6, 4/4), no thin basis, no unmeasured
arms. The defects were real but did not move those numbers.

So the page's story is **not** "we corrected our numbers." It is: *we found six defects in our own
grader and then proved they don't change our published results* — a measurement that survived its
own audit. That is a stronger and more honest claim. Do not overstate it into a memory result.

## 3. What shipped

Two commits on `feat/action-memory-and-benchmark-instrument`, both unpushed:

- `6c397fd feat(memory): executable ActionMemory with a fail-closed capability allowlist` — the
  repo owner's live-recovery work (see §6).
- `196d67f fix(benchmarks): repair the measurement instrument and give the holdouts headroom`

### Core changes (required, not optional)

Structured scoring was blocked two ways, both now cleared in `src/fabri/`:

- `company.py` — `response_schema` propagates from `company.toml` into the compiled **root**
  manager only, validated at compile time. Verified by real compile: root gets it, 0 of 5
  sub-managers do, `AGENT_MEMORY` instructions retained.
- `core/agent.py` — structured validation now runs on the **memory-stripped** answer. Previously a
  company manager's JSON answer failed 100% of the time because the raw `<!-- AGENT_MEMORY -->`
  block was validated as part of the JSON. The `FINAL` trace event still carries the full text so
  memory mining is unaffected (verified).

### Dataset

`benchmarks/datasets/company_memory_experiments.yaml` — all three cases rewritten. Training
establishes a named protocol with an **arbitrary enum vocabulary** and applies branch A; the
holdout supplies evidence requiring branch B. Required scoring moved to `expected.structured`
(deterministic subset equality); prose matching retained only for forbidden/safety and for
archived runs via `legacy_expected`.

**Verified: no injected `response_schema` contains an `enum`, and no expected value appears in any
holdout prompt.** If that ever regresses, the control reads the answers off the schema and the
headroom is gone.

### Tooling

- `benchmarks/agreement/build_label_sheet.py` — blind stratified sample (all raw↔corrected flips,
  then all corrected-fails, then random fill), deterministic under `--seed`. Verified: the
  generated sheet contains no verdict, no rubric term, no filename.
- `benchmarks/agreement/score_agreement.py` — agreement %, Cohen's kappa (dependency-free), a 2×2
  confusion table, and the itemized disagreement list. Fails loudly on unfilled items.

### Site (`fabri-rosters`, uncommitted)

- `schema/benchmarks.schema.json` + `scripts/validate_benchmarks.py`, wired into `validate.py` and
  `build_index.py`. Verified the gate rejects: malformed JSON, missing `subject`, duplicate
  `(subject, benchmark, date)`, `sign_test_p: 1.5`.
- `records` is now append-only history; `(subject, benchmark, date)` unique.
- `web/` — `BenchmarkPanel`, `Sparkline`, history grouping + delta, instrument headline block,
  verbatim agent-output excerpt, per-run COGS table. Browser-verified in both themes.

## 4. Gates (all run, all green)

| gate | result |
|---|---|
| `uv run pytest tests -q -p no:randomly` | **1335 passed, 1 skipped** (from 1289 → **+46 tests**) |
| `npm --prefix web test` | 8 passed |
| `npm --prefix web run build` | clean |
| `python scripts/validate.py` | passed |

**The `-p no:randomly` flag is load-bearing.** Without it, 8–10 tests fail and *which* ones changes
per run (shared qdrant collection state). Pre-existing, unrelated to this work — but it means CI
green is currently not a reliable signal. Worth its own fix.

## 5. What is NOT done — the actual remaining work

Ordered by what blocks what.

### (a) The headroom smoke run — the next real decision point

Everything downstream depends on this. The rewritten holdouts are **paper-designed and unvalidated**.

```
# 2-replica smoke, one company first
FABRI_ROSTERS_ROOT=/Users/rushour0/gba/fabri-rosters \
  uv run python -m fabri.benchmarks.company_memory_study \
    --dataset benchmarks/datasets/company_memory_experiments.yaml \
    --case support_hq_safe_incident_response --replicas 2 ...
```

Three possible outcomes, decide the response **before** running:

- **control fails, memory passes** → headroom is real. Proceed to the full 6-replica run.
- **control still passes** → no headroom. The holdout is not done; rewrite again. Do not spend on
  a full run.
- **both fail** → memory is not retrieving the protocol. That is a different bug (mining or
  retrieval), not a headroom problem. Inspect the retrieved lesson and the final structured object,
  not just the prose.

Cost estimate: ~$2–5 per company for the full run, based on the 2026-07-22 spend
(1.40 / 2.67 / 0.64).

### (b) The human agreement study — needs a human, not an agent

Tooling is built and verified. Nobody has labeled anything.

```
uv run python benchmarks/agreement/build_label_sheet.py \
  --logs-root benchmarks/results/run-logs-2026-07-22 \
  --sample-size 36 --seed 20260722 \
  --out-sheet benchmarks/agreement/label_sheet.md \
  --out-key benchmarks/agreement/label_sheet_key.json
# fill in verdict:/reason: for all 36 items, then:
uv run python benchmarks/agreement/score_agreement.py \
  --sheet benchmarks/agreement/label_sheet.md --key benchmarks/agreement/label_sheet_key.json
```

Until this runs, `instrument.agreement_kappa` stays `null` and the page correctly renders
"Unavailable". **The scorer still has no validity evidence** — six bugs were fixed, but "we fixed
the ones we found" is not the same as "it agrees with human judgement." This is the single
highest-value unfinished item.

### (c) Populate the page's new fields

`instrument`, `excerpt`, and per-record `runs` are all implemented and browser-verified but
**unpopulated** — the committed `benchmarks.json` has none of them. When filling `instrument`:

> `verdicts_flipped: 3` refers to the **evolution archive** (`full-evolution-20260721`, 58 scored
> logs), NOT the three published memory-vs-control records, which did not move at all. Word it so
> that distinction cannot be misread.

`runs` requires per-arm costs, which `build_record` now records.

### (d) Known limitations deliberately left alone

- `score_text("Checkout rollback follow-up.")` still **passes** the Support HQ prose rubric. A
  three-word keyword list clears a published rubric. Not fixable in a substring matcher — the fix
  is the structured rubric, which is why (a) matters.
- "Hi Maya," still fails `Maya Chen`. Alias resolution needs task knowledge the scorer lacks.
- The archive can only ever be prose-scored: **zero** archived runs contain a structured payload
  (verified across 1,521 trace files).

## 6. ActionMemory (commit `6c397fd`) — independently verified

The repo owner ran a live Revenue Ops recovery: 128-token truncation failures progressed to a
632-word sourced brief after mined actions raised only the affected roles' caps. Live spend
$0.211746. Full write-up: `benchmarks/results/revenue-ops-live-failure-curriculum-2026-07-22.md`.

Verification performed here (not taken on the implementer's word):

- **Fail-closed envelope attacked with 10 malformed actions; all handled correctly.** Blocked:
  `bash` capability, unknown/out-of-scope role, `max_tokens` ≠ recorded retry cap, retry cap > 2×,
  retry cap > 32768, stale precondition, non-idempotent policy, `max_attempts: 5`, bool-as-int.
  An attacker-supplied `config` path was **ignored** — the path comes from the trusted manager
  config, never from action data.
- **Rollback verified** by forcing the second write to fail; the first role was restored.
- **Default-off verified**: two independent gates, `memory_action_enabled` and
  `memory_action_apply_enabled`, both `False`.
- Commit is green in isolation: 1266 passed.

Two caveats found:

- **The YAML round-trip destroys comments.** A `# IMPORTANT: hand-tuned` line was silently
  dropped. Harmless for generated compiled configs; data loss if ever aimed at a hand-written one.
- **"Source rosters are never edited" holds by construction, not enforcement.**
  `action_execution` writes whatever path the manager config names; it is compiled paths that make
  the guarantee true. Add an explicit assertion if that property matters.

The write-up's own caveat is correct and should stay prominent: the implementation changed between
episodes and there was no frozen control arm, so this demonstrates the **mechanism**, not the
benefit.

## 7. Decisions taken (so they are not silently re-litigated)

| decision | choice | why |
|---|---|---|
| structured scoring path | fix the core (schema surface + validate-after-strip) | benefits all users; the benchmark-local hack would have left the core bug in place |
| already-published numbers | republish corrected, keep old visible | moot in the end — nothing moved |
| agreement gold labels | human-labeled stratified sample | an LLM judge validating an LLM scorer is not independent evidence |
| dataset key | `expected.structured` | matches the existing setup-probe precedent |
| forbidden-side scoring | stays prose, permanently | safety terms are prose by nature |

## 8. Loose ends

- `.scratch-r1-scorer-audit.md`, `.scratch-r2-holdout-designs.md`, `.scratch-r3-structured-path.md`
  are untracked research reports. R1 (the full scorer audit with every artifact reference) is worth
  keeping; decide keep-as-docs or delete.
- `uv.lock` carries an unrelated 0.19.0→0.19.2 bump, left alone.
- `fabri-rosters` changes are **uncommitted** — schema, validator, and the whole `web/` panel.
- Nothing is pushed in either repo.
