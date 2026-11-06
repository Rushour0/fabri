# Benchmarks & evals: proving (and disproving) the memory bet

Fabri's core strategic claim is *"the self-improving agent runtime with
honest COGS."* That claim is only worth something if you'd actually run an
experiment that could falsify it. This document is a study of the benchmark
and evaluation work built to do exactly that — including the times it said
"no, this doesn't work" or "no, we can't tell yet." That honesty is the point
of the whole exercise, and it's also the most interesting part of the last
two weeks of work.

If you read nothing else, read the "Current state" table near the bottom.

## Why benchmarks exist at all

Fabri's pitch is that an agent using Fabri gets better and cheaper across
repeated runs, because it mines lessons from its own traces into memory and
retrieves them on later tasks. That is an empirical claim, not an
architectural one — the code can be wired up correctly and the claim can
still be false in practice (wrong lessons mined, wrong lessons retrieved,
lessons that don't transfer, or lessons that just don't matter for the task
shape being tested).

`BENCHMARKS.md` states the operating principle directly:

> Fabri keeps setup qualification, retrieval quality, live memory behavior,
> and public memory accuracy as separate evidence tracks so one cannot stand
> in for another.

That separation matters. It would be easy to declare victory because
"retrieval works" (it does — see below) without ever proving that retrieval
*changes outcomes*. Most of this document is about keeping those two things
apart.

## The harness: where the code lives

- **`BENCHMARKS.md`** (repo root) — the living scoreboard. Every benchmark
  row states what it measures, its current status, and links to the full
  result writeup. This is the file to re-read before citing any fabri number.
- **`benchmarks/`** — datasets, run outputs, and published result reports.
  - `benchmarks/README.md` — artifact map and test commands.
  - `benchmarks/datasets/company_memory_experiments.yaml` — the three
    roster-company experiment definitions (Support HQ, Reliability Labs,
    Revenue Ops) used by the qualification and memory-vs-control studies.
  - `benchmarks/results/` — reviewed, publishable markdown + JSON reports.
    Raw per-run traces are intentionally *not* published (private prompts/
    model output), only reviewed aggregates.
  - `benchmarks/runs/` — raw run directories (gitignored/local).
  - `benchmarks/agreement/` — the human-vs-scorer agreement audit (see
    below).
- **`src/fabri/benchmarks/`** — the runner source code:
  - `company_setup_probe.py` — qualifies a compiled company roster against a
    frozen deterministic rubric.
  - `company_memory_study.py` — the memory-vs-control experiment runner
    (train → holdout, memory arm vs. control arm).
  - `company_evolution.py` / `company_memory_report.py` — generational
    memory accumulation (gen-001 → gen-002) and report building.
  - `session_delta/` — the "does cost drop across repeated runs of the same
    task" runner (fabri's own N+1 metric).
  - `retrieval_eval/` — the offline, credit-free retrieval quality gate
    (recall@k / MRR against a labeled fixture). This is the one CI-gated
    metric that runs on every relevant change.
  - `longmemeval/` — a runner for the public LongMemEval dataset, so fabri's
    numbers can be compared apples-to-apples against Mastra/Letta/Zep.
    Shipped but results are still pending (not yet run at meaningful scale).
  - `stats.py` — the Wilson score-interval helper used everywhere a k/N rate
    is reported (added in the statistical-rigor pass, see below).
  - `rescore_runs.py` (in `benchmarks/`, not `src/`) — re-extracts each
    arm's holdout output from its saved trace and re-scores it with the
    *current* rubric, without spending API credits again. This is how the
    evaluator bugs described below were re-verified against 118 archived
    runs for free.
- **`docs/benchmark/index.html`** — the public-facing report page (an
  "academic research-paper treatment" of the results, per the commit
  history) that gets published to fabri.rushour0.com.
- **`docs/retrieval-tuning.md`** — a first-user guide to the retrieval knobs
  (RRF k, hybrid vs dense vs sparse, MMR) that the offline eval gate tunes.
- **`docs/design/benchmark-instrument-handoff-2026-07-22.md`** — a detailed
  handoff doc explaining the scorer-repair work; worth reading in full if
  you want the blow-by-blow of finding the evaluator bugs.

## The qualification studies: does the company even work, before memory enters the picture?

Before asking "does memory help," fabri first asks a more basic question:
does a compiled company (a manager + specialist agents, wired per
`company.toml`) reliably complete its job at all? This is the **company
setup qualification** benchmark, run via
`python -m fabri.benchmarks.company_setup_probe`.

Three roster companies were qualified on 2026-07-20, each against a frozen,
deterministic rubric (does the delegation tree run to completion, does the
output avoid forbidden claims, does it stay in budget):

| company | task | replicas | rubric result | verdict |
|---|---|---|---|---|
| Support HQ | safe incident response | 3 | 3/3 (100%, 95% CI 44–100%) | small-sample pass |
| Support HQ | same task, confirmation | 10 | 9/10 (90%, 95% CI 60–98%) | **does not qualify** |
| Reliability Labs | setup qualification | 3 | 2/3 (67%, 95% CI 21–94%) | does not qualify (underpowered) |
| Revenue Ops | setup qualification | 3 | 0/3 (0%, 95% CI 0–56%) | does not qualify (underpowered) |

**The headline finding here is not that the companies are broken — it's that
a 3-replica gate is a statistically fragile thing to trust.** Support HQ
looked perfect at n=3 (3/3), which is exactly the kind of result that invites
you to ship it and move on. Instead the team ran a 10-replica confirmation,
and it came back 9/10 — one run silently dropped the required follow-up
commitment. That's below the 100% bar the rubric demands. The honest
conclusion published in `BENCHMARKS.md` is blunt: *"at adequate sample size,
none of the three companies clear the 100% qualification bar."*

Reliability Labs and Revenue Ops were only ever run at n=3, so their
confidence intervals are wide enough (21–94%, 0–56%) that "failed" is the
correct call on the frozen rubric, but the *precise* rate at those companies
is still unresolved. Revenue Ops additionally failed for a mundane
infrastructure reason — one run truncated operationally — alongside the
substantive over-claiming failures.

Full reviewed reports:
`benchmarks/results/support-hq-setup-qualification-2026-07-20.md` (and the
10-replica variant), `benchmarks/results/reliability-labs-setup-qualification-2026-07-20.md`,
`benchmarks/results/revenue-ops-setup-qualification-2026-07-20.md`.

Total live spend across the whole 2026-07-20 qualification matrix: about
$0.70 (plus ~$0.27 in earlier pilots/classifier validation). This is
deliberately cheap experimentation — a design choice throughout this work is
to spend real API dollars only once the instrument measuring the result has
been proven trustworthy on paper first.

## The memory-vs-control study, and the pilot that reversed at 10 replicas

This is the study that actually isolates memory's effect on an outcome, as
opposed to setup qualification (which only asks "did the company run").

**Design:** train a company on a related task — it writes learned guidelines
to memory during that run — then run a fresh holdout task twice: once with
that trained SQLite memory copied into a clean compile (the memory arm), once
with an empty memory (the control arm). Both arms are scored against the same
rubric.

The first published result (`47c86c6`, 2026-07-20) is the one worth sitting
with, because it's a case study in why small-sample results are dangerous:

- **3-replica pilot:** memory 3/3 (100%) vs. control 2/3 (67%) — a
  seemingly clean +33pp win for memory.
- **10-replica confirmation of the exact same setup:** memory 7/10 (70%) vs.
  control 9/10 (90%) — a **−20pp reversal**. Memory did not just fail to
  replicate its lead; it came out behind, at essentially equal cost (memory
  was actually $0.0014 cheaper per run).

The commit message calls this what it is: *"an honest and humbling
outcome... The mechanism fires reliably [2 guidelines retrieved on every
memory run, 0 on every control run, every time] but it does not improve
outcomes."* No paired significance test was run on this data, so the
correct reading is not "memory loses" either — it's "underpowered, no
resolved effect either way," and BENCHMARKS.md is explicit that overlapping
Wilson intervals at n=10 do not constitute a hypothesis test.

A second company, Reliability Labs, was run the same way on 2026-07-21 and
showed the same shape: memory 6/10 (60%) vs. control 7/9-as-completed (78%,
or 7/10 conservative) — again a negative-leaning, statistically unresolved
delta.

Full reports: `benchmarks/results/support-hq-memory-vs-control-2026-07-20.md`,
`benchmarks/results/reliability-labs-memory-vs-control-2026-07-21.md`.

### The retrieval-config sweep: config is not the lever (PR #68 / commit `8057ebe`)

The natural next question after a negative-leaning memory result is "did we
tune retrieval wrong?" So the team reran the Support HQ memory arm with
`top_k` raised 5→10 and with `hybrid+mmr` selection instead of the shipped
default. In all three configurations, memory retrieved **the exact same 2
guidelines** every run. The trained memory only ever *contained* about 2
guidelines — you cannot retrieve more evidence than exists in the store, so
no retrieval-side change could possibly move the outcome. This finding was
later locked into CI as a regression test
(`test_retrieval_config_invariance.py`, added in PR #70) so nobody can
"fix" this by quietly tuning `top_k` again without re-deriving the same
result.

That pointed the investigation upstream, to **supply**, not retrieval:

### Root cause found: sub-agent traces were never mined (PR #70 / commit `e1b8830`)

The actual reason trained memory only ever held ~2 guidelines: `process_trace()`
— the only function that mines an agent's trace into memory — was called
only on the top-level `fabri run` CLI path (`cli.py:268`/`:330`). When a
company's manager delegates to specialist sub-agents via
`agent_runner_tool.py`, those sub-agents ran, produced their own traces, and
exited *without ever mining their own trace*. In a manager→specialists
company, every specialist's lessons were silently discarded — only the
manager's own (thin) trace ever contributed to memory. This is documented as
the root cause behind "config is not the lever": you can't fix a supply
problem by tuning demand.

Fix: mine each delegated sub-agent's own trace in `agent_runner_tool.py`,
mirroring what `cli.py` already does at the top level, with mining failures
swallowed and logged so they can never flip a `spawn_subagent` call's exit
code. An `FABRI_DISABLE_SUBAGENT_MINING` env var was added for a future
"mining off" experimental control arm.

## Evaluator honesty: the scorer had four hidden defects, and finding them mattered more than any single result

Before any of the numbers above could be trusted, the tool that *judged*
pass/fail — a rubric-based prose scorer (`score_text`) — needed its own
audit. This is arguably the most important thread in the whole benchmarking
effort, because a broken scorer can manufacture a result in *either*
direction, and both directions happened here.

The defects, found by auditing `score_text` against all 118 archived agent
outputs in `benchmarks/results/run-logs-2026-07-22/`:

1. **Negation false positive (inflated failures).** A correct answer that
   denied a forbidden claim — e.g. *"rollback completion is **not
   confirmed**"* — was flagged as if it had *made* the forbidden claim,
   because the original negation lookbehind was a fixed 60-character
   window and the negating word sometimes sat further back. Fix
   (`4ffa3b7`): scope the negation check to the enclosing sentence (search
   back to `. ; ! ?` or newline, capped at 400 chars), not a fixed
   character window.
2. **"Not only… X" suppressed a genuine forbidden hit** — reproduced
   directly, also part of the negation-window fix.
3. **Substring collisions** — `blame` matched inside `blameless`;
   `verify` did not satisfy a rubric requiring `verification`. Both are
   substring-matching bugs in opposite directions (one deflates, one
   inflates).
4. **Required-term proximity (false negatives on the required side, PR
   `cdaf5f6`).** A live control run wrote a fully correct, customer-safe
   update — *"we will share further customer-facing updates if
   warranted"* — and was scored FAIL solely because the literal phrase
   "further update" never appeared verbatim. That single phrasing miss was,
   by itself, manufacturing an apparent **+100pp "memory beats control"**
   result. Fix: required-term matching became order-preserving proximity
   within one sentence (each word in order, ≤4 intervening words, light
   plural/compound tolerance). The forbidden side was deliberately **not**
   loosened the same way — it keeps exact match plus the sentence-scoped
   negation window — so this fix could not manufacture new false passes in
   the other direction.
5. **Bool/int guard (`329b258`).** Python's `True == 1` / `False == 0` let a
   wrong-typed structured field false-pass on the newer structured-scoring
   path — caught in PR review, not by the audit; fixed by requiring
   bool-ness to match, not just equality.
6. **Publication-path bugs, arguably worse than the scorer bugs themselves:**
   `rescore_runs.py` was grading a multi-company run root against *one*
   company's rubric (Reliability Labs and Revenue Ops arms were being
   scored against Support HQ's rubric), and percentages were being printed
   next to denominators that weren't theirs (Revenue Ops showed
   "n=10 … 100%" when the real basis was 1 scorable arm). Both fixed.
7. **Holdout leak, fail-closed (`c762e3e`).** The entire headroom of the
   memory-vs-control experiment rests on an invariant that used to be only
   hand-verified: no expected/structured answer value may appear anywhere in
   the holdout prompt, or a no-memory control could read the answer straight
   off the prompt and the whole study is void. This is now enforced in
   `load_memory_case` — it raises `ProbeError` at *load time*, before any
   spend, if a holdout prompt leaks an expected answer. All three real
   dataset cases pass the guard today.
8. **True no-memory control (`4ffa3b7`).** Before this fix, there was
   literally no way to fully turn memory off — a "control" holdout still
   mined and retrieved memory within its own run, contaminating the
   baseline. Added `memory.mining_enabled` / `memory.retrieval_enabled`
   config flags (default True), gated at both `process_trace` call sites and
   short-circuited in `_retrieve_inner`; the study now writes `False` into
   every compiled node for control arms and asserts zero guidelines
   retrieved as a sanity check.

The team also ran a **near-miss worth remembering** (documented in
`docs/design/benchmark-instrument-handoff-2026-07-22.md`): rewriting the
rubric's forbidden-term list and then re-scoring old archived runs against
the *new* rubric produced an apparent reversal (Revenue Ops memory
100%→75%, control 66.7%→100%). That would have been publishing a false
"correction" — old runs graded against a rubric that didn't exist when they
ran. The fix was per-case `legacy_expected` so a run is always scored by the
rubric that was live on its date, with rubric provenance recorded alongside
every score. With era-correct rubrics, all three published 2026-07-22
records regenerated **byte-identical** — meaning the defects were real, but
none of them happened to move the numbers that had already shipped. The
writeup's framing of that outcome is worth quoting directly, because it's
the thesis of this whole document: *"the page's story is not 'we corrected
our numbers.' It is: we found six defects in our own grader and then proved
they don't change our published results — a measurement that survived its
own audit. That is a stronger and more honest claim."*

### The agreement study (`benchmarks/agreement/`)

Once the scorer was patched, the team needed evidence that its *pass/fail*
verdicts agree with a human reader's — not just that the code does what the
code was told to do. `benchmarks/agreement/build_label_sheet.py` builds a
blind, deterministically-seeded stratified sample (36 items: every
raw↔corrected flip, then every corrected-fail, then a random fill) into
`benchmarks/agreement/label_sheet.md`, with the answer key
(`label_sheet_key.json`) held back from the human rater. The human enters
`pass`/`fail` plus a one-line reason per item; `score_agreement.py` then
computes agreement %, Cohen's kappa, a 2×2 confusion table, and an itemized
disagreement list. This repo currently has a generated (unfilled) label
sheet and key checked into the working tree (untracked, not committed) —
the labeling pass itself is listed as open work in the headroom-smoke memory
notes ("label the 36 agreement items").

## The measurement-instrument repair and the headroom smokes

Even after the scorer was fixed, there was a second, more fundamental
problem: **the benchmark was originally built so memory could not possibly
help.** Every holdout prompt restated its own rubric — a no-memory control
had everything it needed from the prompt text alone. Two of three companies
sat at 100% either way, p = 1.0 everywhere. No headroom, no experiment. This
is documented in the `docs/design/benchmark-instrument-handoff-2026-07-22.md`
handoff doc, and it's a good example of a benchmark that "passes" while
proving nothing.

The fix rewrote the dataset (`benchmarks/datasets/company_memory_experiments.yaml`):
training now establishes a named multi-branch protocol with an **arbitrary
enum vocabulary** and applies one branch (branch A); the holdout supplies
evidence that requires a *different* branch (branch B). Required scoring
moved to `expected.structured` (deterministic subset equality) rather than
prose matching, with prose retained only for forbidden/safety checks. Two
supporting engine fixes were needed to make structured scoring possible at
all: `response_schema` now propagates from `company.toml` into the compiled
root manager (`company.py`), and structured validation in `core/agent.py`
now runs on the memory-stripped answer (previously a manager's JSON answer
failed validation 100% of the time because the raw `<!-- AGENT_MEMORY -->`
block was being validated as part of the JSON).

### The three-layer diagnosis (2026-07-22 smokes)

With the repaired instrument, three 2-replica smokes of the Support HQ
incident-response case (~$0.81 total) were run — deliberately not scaled to
a full 6-replica run, because the instrument itself hadn't been validated
live yet. The result was genuinely good news on one axis and bad news on
another:

- **Headroom is real.** Control failed all 6 completed arms on the
  rewritten holdout. The old "benchmark built so memory can't help" problem
  is gone — there is now something for memory to actually win.
- **But memory failed every arm too**, for three distinct, independently
  verified engine reasons:
  1. **Transport check was over-strict** — it aborted a replica whenever
     *any* declared memory DB was absent post-training, even when that
     absence was legitimate (a crew that never ran during training has no
     memories to have). Fixed (`cfb2f15`): abort only when *zero* training
     DBs exist.
  2. **The 30-token `guideline_max_tokens` cap physically truncates
     protocol lessons** — a mined guideline was found truncated mid-sentence
     ("Action: Map MITIGATED. Expected outc..."). A `--guideline-max-tokens`
     study-CLI override was added (`9fab860`) so the *study* can raise the
     cap, but the engine's own shipped default was deliberately left
     unchanged — a COGS tradeoff call made by the project owner, not a bug.
  3. **The open finding, still unresolved as of the last recorded smoke:**
     even past the cap, mining stores only the **applied** branch (branch A,
     verbatim) rather than the **declared** full protocol — so even with
     headroom and even with transport working, the memory a holdout retrieves
     never contains the information (branch B) it needs. Retrieval only ever
     injects `entry.text` (`orchestrator/retrieval.py:848`), so there's no
     way to recover branch B from what's stored. A separate self-report
     channel exists end-to-end for this purpose (`<!-- AGENT_MEMORY -->`,
     wired `memory/output.py` → `pipeline.py::extract_agent_memory`) but the
     model emitted it in **0 of 4** training runs despite an explicit prompt
     instruction to do so.

A follow-up "smoke #4" tried hardening the AGENT_MEMORY prompt into a strict
template contract (v0.19.3) — it still produced 0/2 emission in training,
*and* introduced a new structured-output regression (the model embedded the
memory block inside its JSON response value, breaking 3/4 arms). That
attempt was reverted and shipped as v0.19.4. The conclusion drawn from this
failure is important: **convention capture has to be engine-side —
deterministic gating plus typed extraction — not something you can get by
asking the model nicely in a prompt.** This is exactly the insight that
motivated the separate convention-mining v1 initiative (PR #83, v0.20.0,
default-off) that shipped afterward, and its follow-up bugfixes (PR #84,
current HEAD) — mentioned here because it's the direct answer to gap #3
above, though full closure of the loop (does convention mining actually fix
the holdout outcome) was still open as of the last recorded smoke round
(round 7, 2026-07-24): supply and transport now both work end-to-end (a
holdout replica's memory store held both protocol branches, including the
needed branch B), but the model still answered using branch A even while
citing branch-B evidence — rubric 0/4. The suspected last blocker is a
one-line wiring gap (the study only enables `convention_mining_enabled` on
the *training* compile, so the holdout-side branch-selection validator is
never armed). This is recorded as the next step, not yet verified.

## Statistical rigor and CI gates (PR #70)

Beyond the sub-agent-trace root-cause fix described above, this same PR did
a project-wide rigor pass:

- Added a **Wilson score-interval** helper (`src/fabri/benchmarks/stats.py`)
  and applied it to every k/N rubric rate published anywhere in
  `BENCHMARKS.md` and the individual result files. Wilson intervals stay
  well-behaved at small n and at 0%/100% extremes, unlike a naive normal
  approximation — important because most of these studies run at n=3–10.
- Rewrote framing to be **overlap-aware**: at n=10, a memory-vs-control delta
  with overlapping Wilson intervals is reported as "underpowered / no
  significant difference," not as a point-estimate win or loss. No paired
  significance test has been run on any of these results, and the methodology
  section is explicit that CI overlap is not the same thing as a hypothesis
  test.
- Fixed a **denominator overclaim**: Reliability Labs' control arm had
  actually completed 9/10 replicas, not 10/10 — the report was silently
  using the wrong base. Now both readings are published: the as-completed
  rate (7/9, primary) and a conservative full-nominal-denominator rate
  (7/10), with the conservative reading used for any cross-arm comparison.
- Added a **"Statistical conventions"** and a **"Gated vs. ungated
  evidence"** subsection to `BENCHMARKS.md`, explicitly distinguishing the
  two metrics that run in CI as regression gates (offline retrieval eval,
  and the new retrieval-config-invariance check) from everything else, which
  is a one-off, point-in-time, non-automated live measurement that can go
  stale.
- New CI gates: `test_retrieval_config_invariance.py` (locks "config cannot
  conjure evidence not in the store" — i.e. the config-sweep finding above
  can never silently regress) and a hybrid+mmr floor added to
  `tests/test_retrieval_eval_gate.py`.
- Gate at merge time: 1173 passed, 1 skipped (+16 tests, no regressions).

## Reproducibility manifests and the public report page

Every published qualification/memory-vs-control result self-generates a
reproducibility manifest and a schema-validated JSON result alongside its
markdown report (added `65cd9e9`, "self-generate reproducibility manifest +
schema-validated results"). `BENCHMARKS.md` states the rule directly: *"Every
published result must name the applicable config or dataset, Fabri version,
and external roster revision."* This is what lets someone re-run
`company_setup_probe` or `company_memory_study` months later against a
pinned `FABRI_ROSTERS_ROOT` revision and get a comparable number.

`docs/benchmark/index.html` is the public-facing page (published to
fabri.rushour0.com) that presents this evidence with an "academic
research-paper treatment" — history/sparklines per metric, verbatim excerpt
of agent output, per-run COGS table, and (per the fabri-rosters side of this
work, tracked separately) a JSON-schema-validated, append-only
`(subject, benchmark, date)`-unique results feed so the page can't silently
overwrite or duplicate a prior published number.

## Current state — what's proven, what's open, what failed

| item | status |
|---|---|
| Offline retrieval quality (recall@k/MRR) | **Proven and CI-gated.** Hybrid (RRF) is the best strategy on every metric (recall@5 0.938, MRR 0.844); BM25-was-a-no-op bug found and fixed; RRF k retuned 60→20; success-pattern slot front-load bug fixed. Regresses the build if it drops. |
| Retrieval-config invariance ("config is not the lever") | **Proven, CI-gated.** Cannot retrieve more evidence than the store contains, regardless of top_k/strategy. |
| Company setup qualification | **Run live for all three companies; none clear the 100% bar at adequate sample size.** Support HQ 9/10 at n=10 (was 3/3 at n=3); Reliability Labs 2/3 and Revenue Ops 0/3, both underpowered at n=3. |
| Memory-vs-control outcome effect | **No resolved effect, on two companies, on one related-task/holdout pair each.** A 3-replica "memory wins +33pp" pilot reversed to "−20pp" at 10 replicas; no paired significance test run either way. |
| Root cause of thin memory supply (sub-agent traces never mined) | **Found and fixed** (`e1b8830`). Not yet re-measured at scale post-fix. |
| Evaluator/scorer correctness | **Six real defects found and fixed** (negation window, required-term proximity, substring collisions, bool/int guard, publication rubric-mismatch, thin-denominator display bug). Verified against 118 archived runs; published numbers regenerated byte-identical once era-correct rubrics were used. |
| True no-memory control + holdout-leak guard | **Shipped.** Control now provably retrieves 0 guidelines; holdout prompts fail-closed at load time if they leak an expected answer. |
| Headroom (can memory even win on the rewritten holdout) | **Proven real** — control fails 6/6 completed arms on the rewritten holdout. |
| Whether memory can *use* that headroom | **Still open / negative so far.** All memory arms in the headroom smokes also failed, for three found-and-mostly-fixed engine reasons; the live remaining gap is that mining captures the applied branch, not the declared protocol — a prompt-compliance fix for this was tried and reverted (v0.19.4) because it broke structured output. |
| Convention-mining v1 (the engine-side fix for the branch-capture gap) | **Shipped default-off (v0.20.0, PR #83)**, with a follow-up bugfix (PR #84). Supply and transport verified working end-to-end in the latest smoke (round 7); model still picks the wrong branch — rubric 0/4 — with a suspected one-line wiring fix (arm convention checking on the holdout compile, not just training) identified but **not yet verified**. |
| Human-scorer agreement study | **Tooling built** (`benchmarks/agreement/build_label_sheet.py`, `score_agreement.py`, Cohen's kappa); **labeling pass not yet done** — the generated sheet and key exist locally but are untracked/unfilled. |
| LongMemEval (public benchmark, apples-to-apples vs. Mastra/Letta/Zep) | **Runner shipped, no results yet.** |
| Statistical rigor (Wilson CIs, overlap-aware framing, denominator honesty) | **Applied project-wide** (PR #70); codified in `BENCHMARKS.md`'s methodology section. |
| session-N+1 cost delta (fabri's own "gets cheaper" metric) | **One result exists and is explicitly caveated as non-canonical** — ↓7.8% cost on `gpt-4o-mini` on a constructed failure-recovery task, not the canonical Anthropic-Sonnet config. The canonical number is still pending. |

## The honest one-paragraph summary

Two weeks of benchmark work did not produce a clean "memory works, ship it"
headline — and that is by design, not a failure of the work. What it
produced instead is a harness that can be trusted: a scorer with six known
and fixed defects instead of unknown ones, a control that is provably
memory-free, a holdout that provably can't be answered from the prompt, CI
gates that lock in the retrieval-quality wins that *are* real, and — as of
the last recorded smoke — a live, three-layer diagnosis of exactly why
memory still doesn't pay off on the hardest test case, down to a suspected
one-line fix. The retrieval layer's improvements (hybrid default, BM25 fix,
RRF retune, success-pattern reordering) are proven and protected by
regression tests. The bigger claim — that fabri's memory loop makes an
agent company measurably better or cheaper — remains unproven, and the
project's own numbers say so.
