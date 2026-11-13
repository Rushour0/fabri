# The fabri build timeline: what shipped, in order

This is a study document, not marketing copy. It walks the git history of this repo from the
first commit (2026-06-19) to today (2026-07-24) — about five weeks — and explains what got
built, why, how it works, and what is actually proven versus still open. The spine is
`git log --reverse` and `CHANGELOG.md`; the negative results come from `benchmarks/results/`
and `docs/design/`. Where something failed or reverted, it's called out as loudly as the wins.

Fabri is a Python framework for running LLM agents (single agent or multi-agent "companies")
with built-in cost accounting (COGS), a memory/self-improvement loop, sandboxed tool execution,
and a web UI (Studio) to watch runs happen. The core loop lives in `src/fabri/core/agent.py` and
`src/fabri/core/runner.py`; almost everything else — memory, sandboxing, providers, Studio,
connectors — is a layer added on top of that loop over the five weeks below.

---

## Era 1 — Core engine (2026-06-19 → 2026-06-23, v0.1.0 → v0.6.1)

**Headline commits:** `5c3dbb9` initial commit, `4667f76` rename to fabri, `a08986d` v0.2.0
(PR #1), through `e1ecf60` v0.5.0 and `99ea112` v0.6.0.

This is the foundation era. The initial commit was called "agent-memory framework" — memory was
the seed idea, not an afterthought bolted on later. Within the first release cycle the repo
picked up:

- **The agent loop itself** — dispatch tool calls, track outcomes, retry on truncation.
- **`spawn_subagent`** (F1, `459a851`) — the primitive that makes multi-agent fabri possible: an
  agent can dynamically spawn a child agent as a tool call, with its own budget and prompt.
- **`ask_user`** (A1, `ef1170a`) — the human-in-the-loop primitive that Studio's "Questions" inbox
  is eventually built on top of.
- **Sandboxing** (`fabri.sandbox`, S1/S2) — a `Sandbox` ABC plus `LocalSandbox` and a
  `DockerSandbox` with a warm pool, so tool execution (especially `bash`/`edit_file`) doesn't run
  unconstrained on the host.
- **Parallel-aware dispatch** (F2) — multiple tool calls in one turn can fan out concurrently.
- **TOON encoding for tool results** (`7c401be`) — a token-efficient serialization to cut LLM
  spend on tool round-trips; this is the first sign of "COGS" (cost of goods sold) as a design
  constraint, not just correctness.
- **Per-run COGS + frugal base prompt** (v0.5.0, `e1ecf60`) — cost accounting becomes a
  first-class concept, not a debug afterthought.

**Licensing wobble:** v0.6.0 relicensed to Business Source License 1.1 (`99ea112`) — a
non-compete-style license, presumably to protect a commercial angle. This gets reverted later
(see Era 4).

**Lesson:** the project started from "agents need memory to get better over time" and
"multi-agent orchestration needs a spawn primitive and a cost ledger from day one" — both ideas
proved durable and everything since builds on them.

---

## Era 2 — Hardening + providers (2026-06-23 → 2026-06-29, v0.7.0 → v0.8.1)

**Headline commits:** `4588430` v0.7.0 "ship the ten code-gaps the positioning claim depends
on", `9ba03a3` v0.7.7 multi-provider, `5d92744` Gemini default, `f2aea5a` v0.8.0 Track B builder.

A rapid-fire chain of `v0.7.x` patch releases (0.7.0 through 0.7.9) closed out a self-identified
audit backlog (P1/P2/P3 severity tiers show up repeatedly in commit messages — a recurring
pattern throughout the project: ship, then run a structured audit pass, then burn the backlog
down release by release).

- **Multi-provider LLM support** (`5603c1d`) — OpenRouter, and shortly after, native Google
  Gemini (`5015c89`) and AWS Bedrock via the Converse API (`ded220b`, v0.8.2). Gemini briefly
  becomes the *default* provider (`5d92744`) with all provider SDKs bundled.
- **Haiku narrator** (`f642cab`) — a cheap model narrates inter-step updates to the user, an
  early UX idea that Studio later formalizes.
- **Track B builder layer** (`cc74757`, v0.8.0) — ideator, tool-writer, prompt-kit, discovery,
  skills: an attempt at a layer that helps *build* agencies, not just run them. This is the
  conceptual ancestor of the `agency-builder` skill that ships properly in Era 4.
- **Bounded parallel fan-out** (`0d9a50f`, v0.8.1) — `tools.max_parallel_spawns`, a safety valve
  on the F2 parallel dispatch primitive so a buggy agent can't fork-bomb itself.

**Lesson:** once the loop worked, effort shifted to "don't let a single provider or a single
bug class define the ceiling" — providers pluralized and audits became a repeating ritual.

---

## Era 3 — Memory gets real: hybrid retrieval + observability (2026-07-01 → 2026-07-14, v0.9.0 → v0.11.1)

**Headline commits:** `508fd56` v0.9.0 hybrid retrieval, `dc14f32` the Improver, `84e4d39`
retrieval eval gate (M4), `c77a2b1` v0.10.0, `8f16327` v0.11.0 OTel.

This is where memory stops being "store text, retrieve text" and becomes an engineered
subsystem with its own eval methodology, living in `src/fabri/memory/`.

- **Hybrid retrieval** (v0.9.0) — BM25 (sparse) + vector (dense) fusion, MMR diversification,
  temporal decay, domain routing.
- **The Improver** (`dc14f32`, v0.9.0/0.9.2, `src/fabri/ingest/`) — opens the memory-mining loop
  to *any* log, not just fabri's own traces: `fabri.readlogs()` / `fabri ingest` with pluggable
  adapters (jsonl, regex, otel/openai, or a custom `@fabri.adapter`). Deterministic-first ($0
  cost) with an opt-in LLM-synthesis path.
- **Negative result, found and fixed the same release:** BM25 was a **silent no-op** on the
  SQLite backend. `_fts5_query` space-joined tokens, and SQLite FTS5 reads a bare space as an
  implicit AND, so any multi-word query matched nothing — `hybrid`/`sparse` silently collapsed to
  `dense`. This shipped in v0.9.0 and was caught by the eval harness built in the very next
  release (v0.10.0), not by a human noticing broken behavior. That's the pattern this era
  establishes: **build the eval before you trust the tuning.**
- **Offline retrieval eval + CI gate** (M4, `84e4d39`) — `python -m fabri.benchmarks.retrieval_eval`
  runs fabri's real retrieval code over a labeled fixture (40 guidelines / 24 queries), reports
  recall@k/MRR/precision@k, and a pytest gate locks the shipped defaults so retrieval quality
  can't silently regress. Zero API cost — this is the model for later benchmark work.
- **Default flipped `dense` → `hybrid`** (M5, `17b48cc`) — but only after the eval proved hybrid
  actually won on every metric, following two tuning fixes: RRF `k` 60 → 20 (the web-scale
  fusion constant flattened rank over fabri's short two-pool retrieval; `memory.rrf_k`) and
  back-loading guaranteed `success_pattern` slots to the tail instead of the head.
- **Retrieval-decision observability** (M3) and **memory-health in `fabri report`** (M6) — every
  retrieval call gets a structured trace event; `fabri report` gets a memory-health section
  (guidelines in store, strategic share, entry age).
- **OTel export** (X1, `e42879c` module, `8f16327` wired into the CLI) — `fabri traces export`
  and end-of-run auto-export to any OTLP backend (Langfuse, Honeycomb, Datadog...).
- **Cross-collection global-lessons tier** (v0.11.1, `710fa5a`) and **stale-guideline detection**
  (`939247b`, `fabri memory stale`).

**Lesson:** this era is the first real demonstration of the project's eventual house style —
build a cheap, deterministic, offline eval harness *before* trusting a tuning change, and let it
catch your own bugs.

---

## Era 4 — Studio (2026-07-14 → 2026-07-18, v0.12.0 → v0.13.2)

**Headline commits:** `1f6e5f6` relicense to Apache-2.0, `95bacd7`/PR #11 Studio conversational
UI, PR #13 fleet + COGS, PR #14 cached-token billing fix, PR #18 Company view.

Two things happen back to back: the license reverts, and a real UI ships.

- **Relicense back to Apache-2.0** (`1f6e5f6`) — the BSL 1.1 experiment from Era 1 is undone
  "effective immediately and retroactively for all previously BSL-licensed versions." No
  commercial licenses were ever issued under BSL, so this is a clean revert, and the core is
  formally opened to outside contribution (previously core PRs were closed on principle).
- **Outcome-classification trust-gap fix** (`eee01f0`, v0.12.1) — a run where **100% of tool
  calls failed** could still report `success` if the model emitted non-empty final text. Fixed
  with a new `all_tools_failed` outcome and a rule that a parent can never report a cleaner
  outcome than its worst nested child. `agency-kernel.md` now mandates "run the deterministic
  verifier yourself and quote its raw output; never trust `outcome` or the model's own
  narration" as a delivery gate — a policy that recurs through the rest of the project.
- **GPT-5 support** (`8ff29e4`) — OpenAI's GPT-5/o-series reject the legacy `max_tokens` param
  and reject function tools unless `reasoning_effort=none`; both fixed, verified live.
- **`agency-builder` skill** (`47d6453`) ships publicly, installable for both Claude Code and
  Codex CLI — the first packaged "give me one prompt, get a working multi-agent agency" tool.
- **Fabri Studio** (`95bacd7`, PR #11/#12/#13, `examples/studio/`, React+Vite) — a real
  conversational UI over `fabri serve`, growing across three PRs in three days into a
  three-surface app: live conversation with a plan timeline and tool-call cards, a COGS panel,
  multi-turn threads, cancel/retry, run history, and a **Fleet** view (fan out N runs, roll up
  summed cost).
- **Cached-token billing bug** (`7612fd8`, PR #14) — OpenAI and Gemini both include cached tokens
  *inside* their reported `input_tokens`/`prompt_token_count`, unlike Anthropic and Bedrock which
  exclude them. Fabri's pricing code assumed the Anthropic convention everywhere, so cached
  tokens got billed at ~1.10x instead of 0.10x — an **~11x overcharge on the cached portion**
  (about 3x on a warm 80%-cached turn), charged against real user API credits. Fixed with a
  documented invariant in `pricing.py` and a hand-computed money proof in the test suite.
- **Company view** (PR #18, `757d0ef`) — Studio's first agency-graph visualization (Office +
  Org-chart skins), derived entirely client-side from trace events already streamed — no backend
  change. Codex review then caught a **nested-envelope shape bug**: a tool's result envelope is
  `{ok, result, error}` with a sub-agent's own return nested one level under `result`; the graph
  read it one level too shallow, so static specialists silently disappeared from the graph
  (`600314c`).

**Lesson:** shipping fast (three UI PRs in three days) surfaces real bugs fast too — both the
billing overcharge and the envelope-shape bug were caught by review/tests within the same week
they shipped, not months later.

---

## Era 5 — Distribution: 0.14 (2026-07-18)

**Headline commits:** `c07ee41` `fabri new agency` scaffolder, `59cd417` examples-in-wheel,
`669c623` `fabri studio` same-origin serve, PR #25 → `8f71aeb` v0.14.0, `7f55acf` v0.14.1.

The question this era answers: can `pip install fabri` alone get someone from zero to a running,
watchable multi-agent agency, without cloning this repo or having Claude Code installed?

- **`fabri new agency <name> --template bug-crew|changelog|blank`** — scaffolds a working parent
  + specialist config set + sandboxed workspace fixture, with paths correct out of the box.
- **`fabri studio`** — serves the *bundled* Studio UI and the run API from one same-origin
  server (no Vite dev proxy needed): `pip install "fabri[sqlite]" && fabri studio`.
- **Examples ship inside the wheel** (`fabri examples`), and the `agency-builder` skill becomes a
  single canonical source with thin per-platform briefs for Codex/Grok/Hermes.
- **Bug found one day later** (`7f55acf`, v0.14.1): scaffolded agencies hardcoded their memory
  `collection`/`sqlite_path` (e.g. `bug_triage_crew_parent`), so two agencies scaffolded from the
  same template shared one memory store and **cross-contaminated each other's learned lessons**.
  Fixed with an `__AGENCY_SLUG__` placeholder the scaffolder fills per-install.

**Lesson:** distribution surfaces a class of bug that single-agency development never would —
shared global state (a hardcoded DB path) is invisible until two independent installs collide.

---

## Era 6 — Company org charts, catalog, HITL (2026-07-18 → 2026-07-19, v0.15.0 → v0.16.4)

**Headline commits:** `3332340` company.toml compiler (v0.15.0), `d4b72ff` catalog mode
(v0.16.0), `de2992a` COGS rollup fix (v0.16.1), UI polish chain through v0.16.4.

- **`company.toml` declarative multi-level org compiler** — a flat file with `report_to` edges
  compiles to a nested `tools.agents[]` tree; no runtime change needed since the engine already
  recurses through that structure. `fabri studio --company` serves the whole org with a live
  COGS overlay on the chart.
- **Questions inbox** (`cdf52a7`) — a cross-run human-in-the-loop dashboard for every pending
  `ask_user` call, plus an optional Slack notify (`f79204d`).
- **`fabri new agency --from <source>`** (`f4333a2`) — pull an agency from a registry (local path
  or `gh:owner/repo/subpath@ref`), feeding the separate `fabri-rosters` catalog repo.
- **Studio catalog mode** (v0.16.0) — Studio becomes a control plane: pre-install/compile every
  agency/company in a roster directory and browse+run any of them.
- **COGS rollup bug** (`de2992a`, v0.16.1) — a parent run's `total_cost_usd` only summed
  *dynamically spawned* children; **static** `tools.agents[]` specialists (and recursively, a
  whole company org tree) were silently excluded from the reported cost. This one is worth
  sitting with: for weeks, any static-specialist agency under-reported its true COGS.
- **Optional embeddings extra** (v0.15.1) — `sentence-transformers` moved out of the base install
  so a plain `pip install fabri` stays lean.
- A run of small Studio UI/UX polish releases (0.16.2 tab URL-tracking, 0.16.3 stable-width
  frame, 0.16.4 depth/blue-accent/micro-motion) closes out the era.

**Lesson:** cost rollup bugs are quiet — the COGS number looked plausible, just wrong, for as
long as nobody compared it against a hand-computed total. This is the second billing-adjacent bug
in two eras (after the cached-token one in Era 4).

---

## Era 7 — Repo agent: 0.17 (2026-07-20)

**Headline commit:** `708f6eb` `fabri repo`, PR #45 → `6d58812` v0.17.0.

- **`fabri repo suggest-prompt`** reads an agent's *promoted* strategic guidelines (lessons that
  recurred across ≥3 sessions) and opens a deduplicated GitHub issue proposing they get folded
  into the agent's prompt — making the self-improving memory loop **visible and reviewable** in
  a normal PR/issue workflow instead of a black box.
- **`fabri repo issue`** — files/refreshes a tracking issue with a hidden marker so re-runs
  comment instead of duplicate-spamming.
- Deliberately GitHub-Action-native: runs from a single workflow file using only the built-in
  `GITHUB_TOKEN`, no model API key, no server to host. This is the "self-improving in your own
  repo, for free" pitch. A live reference repo
  (`Rushour0/fabri-repo-demo`) files its own self-improvement issue weekly.

**Lesson:** the project's positioning shifts here from "framework you use" to "engine that
improves your repo for you" — the connectors work in Era 10 continues this thread.

---

## Era 8 — Auth, repo providers, durable run store: 0.18.x (2026-07-20)

**Headline commits:** `45059c7` Studio login, PR #51 GitLab/Bitbucket, `64f7ee2` durable run
store, `5d06780` v0.18.0, chain through `7248ea5` v0.18.5.

- **Opt-in email+password auth for Studio** (`45059c7`, off by default via
  `FABRI_AUTH_ENABLED`/`FABRI_AUTH_SECRET`) — scrypt-hashed passwords, HMAC-signed HttpOnly
  session cookies, per-run ownership checks. An adversarial security pass (`22ecd33`) hardens
  tenant isolation right after the feature lands, then a same-day follow-up (`17ea16e`) protects
  the catalog endpoint and fixes a run-ownership gap the first pass missed.
- **`fabri repo` gains open-PR support plus GitLab and Bitbucket adapters** (`caa5d80`) behind a
  common repo-provider interface — no longer GitHub-only.
- **Durable, queryable run store** (`64f7ee2`) — the dashboard data spine that later lets Studio
  replay finished runs after a server restart (this actually lands properly in Era 10,
  `2c3dbaa`).
- **Durable company memory** (v0.18.3) — compiled companies get an explicit root
  institutional-memory collection and record a postmortem per company task.
- **Benchmark infrastructure begins in earnest** (v0.18.4/0.18.5): `benchmarks/` gets its first
  reproducible company setup-qualification probes (Support HQ, Reliability Labs, Revenue Ops),
  each with training/holdout prompts and deterministic rubrics, plus the "task precedence" fix
  (`ee93c38`) making sure retrieved memory can never override the user's current instruction.

**First negative result of the benchmark era, and it lands here:** the Support HQ setup
qualification study's **preliminary 3-replica pilot showed memory winning (3/3 vs control 2/3,
+33pp)**. A 10-replica confirmation **reversed** it: memory 7/10 (70%) vs control 9/10 (90%), a
**−20pp** result at essentially equal cost (`benchmarks/results/support-hq-setup-qualification-10replica-2026-07-20.md`).
The project's own writeup calls this "the same small-sample fragility seen in the setup
qualification study" — a direct, self-caught demonstration of why n=3 pilots don't generalize.

**Lesson:** small-sample pilots lie, in either direction. The project's own memory feature
looked like a clear win at 3 replicas and a clear loss at 10 — same code, same company, just more
data. Every benchmark result after this point insists on double-digit replica counts and paired
significance tests, not confidence-interval eyeballing.

---

## Era 9 — Benchmarks: 0.18.x results → 0.19.x (2026-07-20 → 2026-07-21)

**Headline commits:** `6b195c1` "none clear the 100% bar", `47c86c6` "pilot reversed at 10
replicas" (memory-vs-control publication), `a198523` v0.19.0.

This era is almost entirely negative-result publication, and that is the point — the project
treats a null result as a shippable deliverable, not a thing to quietly bury.

- **Qualification matrix published live** (`6b195c1`) — across the candidate companies, **none
  clear the deterministic 100% completion bar** required before a memory-vs-control comparison
  is even meaningful. Ineffective config candidates get rejected *before* any model spend
  (`benchmarks/support-hq...`, `reliability-labs...`, `revenue-ops-setup-qualification` files).
- **Memory-vs-control study published** (`47c86c6`) — this is the "pilot reversed at 10
  replicas" result described above, formalized into `support-hq-memory-vs-control-2026-07-20.md`.
- **Second company confirms no benefit** (`cea1311`, 2026-07-21): Reliability Labs
  memory-vs-control also shows **no benefit** — worth noting the control arm there also had data
  quality problems (9 of 10 scheduled control replicas actually ran).
- **Retrieval-config sweep: "config is not the lever"** (`8057ebe`) — a full sweep over
  `top_k`/strategy for the memory study concludes tuning retrieval parameters doesn't move the
  outcome; whatever gap exists isn't a knob problem.
- **Structured-field scoring + optional frozen LLM judge** (`bf2bd40`) — deterministic
  `expected.structured` assertions gate qualification; an *advisory*, pinned/temp-0/versioned
  judge exists but is explicitly kept out of qualification and cost decisions, guarding against
  judge-drift silently changing what "pass" means over time.
- **Company memory generational-evolution study** (`67d4e28`, later `company-generation-evolution-2026-07-22.md`)
  — trains a company, snapshots its memory, trains a *second* generation on top, and compares
  under a promotion gate (no rubric regression, ≥10% cheaper or ≥25% fewer retries to promote).
  Initial read: a second generation *hurt* — but a later correction (see Era 10) found this was
  itself a rubric-negation bug, not a real regression.
- **v0.19.0** (`a198523`) ships the automated `company_memory_study` runner, self-generated
  reproducibility manifests, and schema-validated results as one coherent release.

**Lesson:** across three companies and multiple studies in this era, fabri's memory feature — the
thing the whole project is named after — could not be shown to help. That result is published,
not hidden, and drives the redesign work in Era 10.

---

## Era 10 — Memory-action pivot + the instrument-bug saga: 0.19.1 → 0.19.4 (2026-07-22 → 2026-07-23)

**Headline commits:** `4ffa3b7`/`eca5225` v0.19.2 rubric fix, `cd9e08f` v0.19.3, `482e9aa`
**revert**, `6a3d0a0` v0.19.4.

This is the most important era to understand carefully, because it's where the project discovers
its own benchmark harness was lying to it, fixes that, tries a targeted feature fix, and then
**reverts that fix one day later** based on live evidence it didn't work. Read
`docs/design/memory-action-PART1-plan.md` and `docs/design/convention-mining-research-2026-07-22.md`
for the full research trail.

- **v0.19.1** (`221c7dd`) — immutable company generations with a real promotion gate, training-
  lesson verification (`rubric_verified` vs `tool_verified`), and a consolidated memory report.
- **The rubric was broken in *both* directions** (v0.19.2, `4ffa3b7`/`eca5225`): forbidden-term
  matching used a fixed 60-character negation lookbehind, so a real denial like "no evidence a
  fix was deployed" scored as a **violation** (false positive), while a negation cue in a
  *previous* sentence wrongly exempted a real violation (false negative). Required-term matching
  was naive substring, so a correct answer phrased differently ("share **further customer-facing
  updates**" vs the literal required string `further update`) scored as **missing**.
  Consequence: **both** the earlier "memory doesn't help" headline *and* this study's own initial
  "+100pp memory win" turned out to be scoring artifacts, not real effects.
  `benchmarks/results/memory-vs-true-control-2026-07-22.md` documents the fix.
- **A "control" was never actually a control.** It started with no transported memory DB but
  still mined and retrieved its own lessons *within the same run* — contamination the study could
  only detect after the fact. `memory.mining_enabled` / `memory.retrieval_enabled` (both default
  `True`) are added as real off-switches, gating both `process_trace` call sites and
  short-circuiting retrieval before any embedding/store query.
- **Re-measured with the real fixes: no significant effect, on either side.** 36 arms, $4.82
  spent. "No significant memory effect on quality or cost on any of three companies... Both the
  prior 'memory hurts' headline and this study's own initial '+100pp memory wins' were scoring
  artifacts." This is the project's most carefully-controlled memory result to date, and it's
  still a **null result** — not because memory is useless, but because the studies run so far
  can't detect an effect either way with a clean instrument.
- **ActionMemory + shadow action detection** (v0.19.2) — a first step toward a different kind of
  memory: instead of retrieving prose "guidelines," mine a *typed, executable* resolution
  (`{problem_signature, scope, preconditions, steps, postconditions, rollback, evidence,
  policy}`) from a run that hit `max_token_retries`, stored quarantine-tier. A `detect_proposed_actions`
  shadow call (log-only, never executed) runs behind `memory.memory_action_enabled` (default off).
- **v0.19.3 — the `AGENT_MEMORY` hard-contract attempt** (`cd9e08f`): live benchmark smokes with
  `gpt-5.6-terra` showed the model dropped the `<AGENT_MEMORY>` self-report block entirely in
  **0 of 4 training runs** when the task prompt demanded its own output shape (e.g. "return the
  ticket response and its compact decision record"). The fix: rewrite `company.py`'s memory
  instructions to state the block is a **required** part of every successful response regardless
  of task format.
- **v0.19.4 — reverted the very next day** (`482e9aa`, `6a3d0a0`): the hardened wording **did not
  increase block emission** (still 0 of 2 training runs in a follow-up smoke) and *caused a new
  regression* — models started embedding the memory block inside the JSON response value itself,
  failing schema validation in **3 of 4 arms**. The chosen fix stopped trying to get the model to
  comply with a prompt instruction and instead moves convention capture **engine-side** — see
  `docs/design/convention-mining-research-2026-07-22.md` — because prompt compliance alone
  wasn't reliable enough to build on.
- **Headroom is real, but the miner is capturing the wrong thing** (`headroom-smoke-2026-07-24.md`
  and the -diagnosis- doc from 07-22): control arms complete at 0/4 across every completed round,
  confirming there IS room for memory to help. But stored lessons "carry the applied branch
  verbatim" — if a training run took branch A of a multi-branch protocol, the miner stores "do A,"
  and a holdout that needs branch B gets no useful guidance even though 6-7 "relevant" guidelines
  were retrieved. **The problem isn't retrieval, it's what gets mined.** This is the direct
  motivation for the convention-mining work in Era 11.

**Lesson:** this is the project's clearest self-correction. It shipped a feature fix
(v0.19.3), measured it live the same week, found it didn't work and introduced a new failure
mode, and reverted within 24 hours rather than defending the sunk cost. That's the intended
reading of this era, not "the team flip-flopped."

---

## Era 11 — Connectors + convention mining v1: 0.20.0 (2026-07-23 → 2026-07-24)

**Headline commits:** `94070ba` connector primitives, `d9444a7` convention mining v1 (PR #83),
`88c09ef` v0.20.0.

- **Convention mining v1** (`d9444a7`) — the designed answer to the Era 10 diagnosis. Per the
  ratified `docs/design/convention-mining-decisions-proposed-2026-07-22.md`, this captures
  **declared multi-branch protocols** (not just "what happened," but "here is the full decision
  table for next time") behind fail-closed gates, first scoped narrowly to
  `effect_class="response_mapping"`. Default-off. Follow-up hardening the same day: an
  effect-class allowlist for raw configs and "approve only admissible conventions" (`b172a08`,
  PR #84).
- **Connector primitives** (`94070ba`) — a shared SSRF-defense module, a `provider:handle`
  credential store, and webhook/Slack/SMTP recipe tools, plus a GitHub-via-MCP config doc. This
  is infrastructure for agents that act *inside* a company's existing tools (Slack, email,
  webhooks), continuing the Era 7 "self-improving in your repo" thread outward to other systems.
- **First cross-session guideline reuse now visible in the actual metric**
  (`92e4e80`) — `guideline_reuse_rate` previously undercounted; fixed to count the *first*
  cross-session reuse, and company memory DBs are anchored durably outside the compiled tree so
  they survive recompiles.
- **`AGENT_MEMORY` becomes a real leak-proof tag** (`525e8a8`) — parsed and stripped so it never
  leaks into the user-visible response, with the org chart in Studio also fixed to show nested
  crew-member roles at deeper company levels.
- **Studio replays finished runs after a server restart** (`2c3dbaa`) — the durable run store
  from Era 8 finally gets wired to replay persisted traces, not just live-stream them.
- **v0.20.0 cut** (`88c09ef`).

**Post-0.20.0 (not yet released as of 2026-07-24):** the branch continues past the 0.20.0 tag
with `feat/evolving-profile-and-application-hardening` (PR #85) and two `effect_class`
normalization fixes (PR #86, PR #87) making `effect_class` an **engine constant** rather than a
model-authored field (`1a9a121`) — i.e. removing one more place where the model's own text could
silently steer a safety-relevant classification, continuing the "don't trust the model's
self-report" theme from Era 10.

**Lesson:** convention mining v1 is the project's first attempt to fix the *supply side* of
memory (what gets stored) rather than the *retrieval side* (how it's found) — a direct pivot
from five weeks of evidence that retrieval tuning wasn't the lever.

---

## Where the project stands today (2026-07-24)

**Proven and shipped:**
- The core multi-agent loop, sandboxing, parallel fan-out, and per-run COGS accounting
  (`src/fabri/core/`) — stable since Era 1-2, no reverts.
- Multi-provider LLM support (OpenAI including GPT-5, Gemini, Anthropic, Bedrock, OpenRouter).
- Hybrid (BM25+vector) retrieval with an offline eval gate that has caught real bugs
  (`src/fabri/memory/`, `fabri.benchmarks.retrieval_eval`).
- Fabri Studio — a real, browser-verified UI: conversation, Company org-chart, Fleet fan-out,
  Questions HITL inbox, catalog browsing, opt-in auth, durable run replay.
- Distribution: `pip install fabri` → `fabri new agency` / `fabri studio` / `fabri examples`,
  no source checkout required.
- `fabri repo` — self-improving-in-your-repo via GitHub/GitLab/Bitbucket, GitHub Actions-native.
- A real benchmark methodology: paired significance tests, reproducibility manifests, fail-closed
  no-spend rejection of ineffective configs, an advisory (never gating) LLM judge.

**Open / unresolved as of today:**
- **Does fabri's memory actually make an agent better?** After the most carefully controlled
  study to date (fixed rubric, real no-memory control, 36 arms, $4.82 spend), the honest answer is
  **no significant effect measured either way** on three tested companies. The current best
  explanation (headroom-smoke rounds 1-4) is that retrieval works fine but **mining captures the
  applied branch of a decision, not the full decision table** — convention mining v1 (Era 11) is
  the first shipped attempt at a fix, but it's narrowly scoped (`response_mapping` only),
  default-off, and unratified against a full-scale replica study as of this doc.
  See `docs/design/convention-mining-decisions-proposed-2026-07-22.md` and
  `benchmarks/results/headroom-smoke-2026-07-24.md`.
- **The `AGENT_MEMORY` self-report channel is unreliable.** Prompt-level compliance (both the
  original wording and the v0.19.3 "hard contract" wording) produced 0/4 and 0/2 emission rates
  respectively in live smokes; the project's response is to stop relying on model compliance and
  do convention capture engine-side instead.
- **Post-0.20.0 work is in flight, unreleased**: `effect_class` is being hardened from a
  model-authored field into an engine constant (PR #86/#87/#88/#89, latest commit `3cb3778`),
  and an "evolving profile" feature (PR #85) has landed on main but not yet been cut as a
  release version.
- Convention-mining v1's real-world lift has not been benchmark-confirmed yet — the headroom
  smokes that motivated it are 2-replica smokes explicitly marked "directional only," and the
  doc says not to spend on a full 6-replica run until a supply-side fix is validated to actually
  move the needle.

**The honest one-paragraph summary:** fabri's engine, orchestration, cost accounting, and Studio
UI are solid and shipped incrementally with very few regressions. Its *namesake* feature — agents
that get better over time via memory — has been rigorously benchmarked five separate times over
two weeks (Era 8-11) and has not yet been shown to help, with two of those "wins" turning out to
be measurement artifacts the project found and fixed itself. The current bet, shipped as v0.20.0
convention mining, is that the fix is in *what gets mined* (full decision tables instead of the
one branch that happened to fire), not in retrieval tuning — and that bet is still open.
