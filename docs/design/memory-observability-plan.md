# Memory-Retrieval Quality + Real Observability — Build Plan

> **Companion:** `docs/design/external-memory-patterns.md` surveys how Hermes
> Agent and OpenClaw handle memory and adds four recommendations (R1 MemoryStore
> Protocol, R2 consolidation pass, R3 always-injected core-digest tier, R4 finish
> the OTel wiring) that build on the M3–M6 / X1 units below.
>
> **Status:** decided plan, not yet built (scoped 2026-07-07).
> **Tracks:** M (retrieval quality) + X (observability). Roadmap cards
> `M3`–`M6` and an updated `X1` cross-reference this doc; this doc is the
> authoritative detail (waves, decision table, resolved blockers, open
> questions). Produced via a Scope→Design→Verify→Synthesize multi-agent pass;
> every unit design was adversarially verified against the real files before
> synthesis (all five came back `needs_changes` — the fixes are folded in
> below).

## 1. Executive decision

Fabri **keeps its homegrown JSONL event spine as the single source of truth and
adds a thin, off-by-default OpenTelemetry export shim on top** — not a bespoke
external protocol and not a Langfuse-SDK dependency. This honors the existing
CHANGELOG "no Langfuse/Agnost dep" decision: we own the *events*, OTel owns the
*export*, and Langfuse is reached for free as just another OTLP endpoint+headers
target. **LangGraph is cut** — it is a DAG runtime, irrelevant to Fabri's ReAct
loop.

**The observability-before-quality ordering holds and is load-bearing.**
Retrieval quality is currently *unmeasured* (`session_delta` measures cost-drop,
`longmemeval` was never run at scale), so every quality change is vibes until we
can both *see* what retrieval decided (unit A) and *measure* recall/MRR offline
(unit C). Therefore the build runs **A (debug surface) + C (measurement gate)
first, in parallel** (C does not depend on A); then the export pipe (B) and
metrics surface (E) land alongside; and **only then** do any unit-D quality
upgrades ship — each opt-in, each gated on a measured C delta, nothing flipped
blind. The one nuance encoded everywhere: the default backend is Qdrant without
`rank-bm25`, so a naive hybrid default-flip silently degrades to dense — D3 is
staged in `benchmark.yaml` and gated on both a positive C delta and backend
capability, never flipped unconditionally.

The units map to roadmap IDs: **A → M3**, **B → X1** (updated), **C → M4**,
**D → M5**, **E → M6**.

## 2. Per-unit direction

### A · Retrieval-decision observability (M3) — BUILD
Emit **one** structured `retrieval` event per call, built inside
`_retrieve_inner` and written via the existing `log_event`/JSONL spine. Zero
model-token cost (trace-only, never enters the prompt), ~1–2KB/run, gated to
`if session_id is not None`.
- **Crash fix (A3):** `sparse_results` is bound only inside the hybrid/sparse
  branch — an unconditional end-of-function reference `NameError`s on the
  default dense path. Initialize `sparse_results=[]`, `sparse_available=False`,
  `sparse_fallback=False` *before* the strategy branch; dense emits
  `sparse_pool_size=0`/`sparse_backend=None`.
- **store_count:** bind `store_count = store.count()` once (retrieval.py:379) and
  branch on the variable — no second network round-trip.
- **Volatile captures:** `embedding_ms` (via `monotonic()` around the `embed()`
  at ~389) and `mmr_pool_before` (before `_apply_mmr` at ~488) captured inline
  regardless of session_id (ns-level cost); only the large dict + candidate list
  build under the `if session_id` guard.
- **inclusion_reason:** keep the original `tag_hit`/`success_pattern`/`base`
  reason; add a separate `mmr_survived` boolean instead of overwriting. Document
  that `final_score` under MMR is *marginal* (can be negative — not a similarity
  score) so E/B never cross-compare.
- **Tests (A7 = net-new):** the claimed harness in `test_unit_hybrid_retrieval.py`
  does not exist (that file only tests pure helpers). Build a `MagicMock` store
  (`count()>0`, `query_by_vector` → `(MemoryEntry, float)` tuples, no
  `query_bm25` attr for dense) **and monkeypatch
  `fabri.orchestrator.retrieval.embed`** to a fixed 384-dim vector (no 44MB model
  download). Isolate traces with `monkeypatch.setenv("FABRI_HOME", tmp_path)`
  (no tmp-home fixture exists in conftest). Include a **dense-strategy case**
  (asserts no `NameError`) and a `session_id=None` negative case (asserts empty
  trace).
- **Deferred (A4b, phase 2):** full dense/sparse per-candidate provenance +
  per-dropped-candidate records behind a new `memory.debug_retrieval` flag
  (default False). Build only when C/D need it to debug a specific regression.

### B · External trace export (X1) — BUILD OTel exporter
v1 is a **post-hoc batch exporter**: `observability/otel.py::export_trace(session_id, otel_cfg)`
reads the finished trace via `read_trace()`, walks events into an OTel span
tree, force-flushes OTLP — fired once at run end after the USAGE event, guarded
by `if otel_cfg.endpoint`, plus a `fabri traces export <session_id>` CLI verb
for back-export.
- **Build order (corrected):** `B1 → B5 (extra + lazy-import scaffold) → B4
  (config + run_config threading) → B2 (driver + wiring) → B3 (mapping) → B6`.
  B2.deps=[B1,B4,B5], B5.deps=[B1]. (Original card had reversed/missing edges —
  the driver's `if otel_cfg.endpoint` guard needs the B4 config field and the B5
  importable SDK first.)
- **Mapping:** START→root `fabri.agent_run` span; STEP_*→child `fabri.step`;
  TOOL_*→grandchild tool spans; THOUGHT/NARRATION/etc.→span *events* on the
  enclosing step. Because it consumes whatever is in the trace, **unit A's
  `retrieval` event exports automatically** as a span with per-candidate attrs —
  B needs zero rework when A lands.
- **Sub-agent nesting (best-effort):** there is **no spawn-session-id field** on
  any event. Parse the child sid best-effort from a *successful* spawn
  `tool_call` result payload (`result.session_id`/`result.trace_path`), then
  `read_trace(child_sid)` + recurse. Crashed children emit `COST_UNACCOUNTED`
  with no child sid → unlinkable leaf span carrying a "cost unaccounted" attr.
  v1 accepts silent incompleteness for failed spawns.
- **Config/deps:** `observability` block in DEFAULT_CONFIG
  (`exporter/otlp_endpoint/otlp_insecure/otlp_headers/service_name`), threaded
  via `run_config.as_kwargs`; env `FABRI_OTLP_*` in `_apply_env_overrides`.
  Optional extra `otel = ["opentelemetry-sdk>=1.20","opentelemetry-exporter-otlp-proto-grpc>=1.20"]`,
  lazy-imported inside `export_trace`; clear `pip install fabri[otel]` error if
  endpoint set but extra missing.
- **Langfuse (B6):** no code path — set `otlp_endpoint` to Langfuse's OTLP ingest
  URL + `otlp_headers` bearer/basic from env. Ship as a documented recipe.
- **Single-file discipline:** B2/B3/B5 all touch `observability/otel.py` — build
  strictly sequentially.
- **Cut:** LangGraph, direct Langfuse SDK, bespoke external backbone.
  **Phase 2:** live inline span tap (B9).

### C · Retrieval eval harness (M4) — BUILD (the ground-truth gate)
Fast, deterministic, offline retrieval-only eval in CI, zero API credits, backed
by in-process `SqliteMemoryStore` (local MiniLM embeds + native FTS5 BM25, no
Qdrant service). A regression tripwire, not an aspirational bar. **No dependency
on unit A; hard prerequisite for all of unit D.**
- **CI install (C5):** the eval store's hybrid/sparse uses **native FTS5**
  (`hasattr(store,'query_bm25')`) — `rank-bm25` is only the *Qdrant* client-side
  fallback and is NOT needed here. Minimal correct change:
  `pip install -e ".[dev,sqlite]"`. Without `sqlite-vec` the gate **SKIPS via
  `pytest.importorskip('sqlite_vec')`** (established pattern) — it does *not*
  fall back to dense.
- **Duplicate-id corruption (C3):** `MemoryEntry.id` is a hash of
  `namespace::text.lower()`; two corpus entries with identical text in one kind
  collapse under `INSERT OR REPLACE`, corrupting ground truth. Runner must assert
  corpus `text` is unique within a kind and assert `store.count()==len(corpus)`
  post-upsert before querying.
- **Determinism (C4):** `query_by_vector` orders by distance with no tiebreak —
  near-equal cosines jitter MRR near the floor. Add a **secondary sort key
  (entry.id)** on result mapping, and set the gate floor at
  **measured-baseline-minus-margin (~−0.05)**, never exact equality (protects the
  recently-restored green suite).
- **Coverage claim:** `_retrieve_inner` is called with no `tool_names`, so
  `mentioned_tools` is always empty — the tag-slot coverage claim is false as
  written. Drop it for v1; scope the fixture to dense/hybrid/domain coverage.
- **Shape:** `tests/fixtures/retrieval_eval.json` (40–60 guidelines, 20–30
  queries, binary relevance); pure metrics module (`recall@k`, `MRR`,
  `precision@k`, stdlib-only, div-by-zero guards); runner; pytest gate under the
  existing `pytest tests/ -q`; `python -m fabri.benchmarks.retrieval_eval`
  head-to-head strategy CLI (the unit-D before/after tool).
- **Cut:** auto-gen / graded relevance / nDCG (reintroduces API cost + labeling
  burden).

### D · Retrieval quality upgrades (M5) — ALL DEFERRED, each gated on C
Nothing ships enabled-by-default in v1. Each is an opt-in config flag whose
default-flip is gated on a measured C delta. Order after C: **D3 → D1 → D4 → D2.**

> **Post-flip fixes (2026-07-07, eval-driven).** Running the C eval head-to-head
> after the D3 flip surfaced that hybrid only won at recall@5 while *losing* at
> recall@1/@3. Two fixes, both measured on the C fixture, made hybrid dominate
> every metric: **(1) RRF `k` 60 → 20** (`memory.rrf_k`) — the web-scale constant
> flattened rank over fabri's short two-pool fusion; recall@3 0.60 → 0.90. **(2)
> `success_pattern` slots back-loaded** — they were front-loaded into ranks 1-2
> ahead of the most relevant guideline, capping recall@1; relevance now owns the
> head and the guarantee fills reserved *tail* slots (recall@1 0.13 → 0.58, MRR
> 0.45 → 0.84, and it lifted dense identically — the front-load hurt all
> strategies). Threading `session_id` into retrieval (M3) also exposed a
> **START-before-retrieval trace-ordering bug**: the run's root `start` event was
> logged *after* retrieval, so the `retrieval` event landed first; `start` now
> emits before retrieval. New first-user tuning guide: `docs/retrieval-tuning.md`.
- **D3 (flip dense→hybrid+mmr):** machinery exists; stage
  `retrieval_strategy: hybrid+mmr` in `configs/benchmark.yaml` **only**; run C
  dense-vs-hybrid-vs-hybrid+mmr; flip the two default sites (`retrieval.py`
  RetrievalConfig default, `config.py` DEFAULT_CONFIG) in a follow-up **only** on
  a positive delta outweighing +30–80ms/retrieval. **Critical caveat:** on stock
  Qdrant without `rank-bm25`, hybrid silently degrades to dense
  (retrieval.py:~418) — gate the real flip on **backend-capability detection**,
  not an unconditional flip.
- **D1 (cross-encoder reranker):** no new dependency —
  `sentence-transformers` (core dep) ships `CrossEncoder`; lazy singleton
  mirroring `get_model()`. **Slot-guarantee fix:** the candidate pool is
  deliberately front-loaded with tag-hit + success_pattern guaranteed slots;
  reranking the whole pool then trimming would evict them, defeating
  `tag_hit_score_floor`/`success_cap`. **Rerank only the base_results/filler
  portion, keep guaranteed slots pinned.** Add
  `cross_encoder_enabled=False` + `cross_encoder_model='cross-encoder/ms-marco-MiniLM-L12-v2'`
  to RetrievalConfig + from_mem_cfg + DEFAULT_CONFIG.
- **D4 (embedding upgrade):** target `BAAI/bge-small-en-v1.5` (stays 384-dim, no
  vector-space break; a re-embed migration is still required). Thread the runtime
  model name into the **existing** per-entry `model_version` field
  (schema.py:27) — don't claim to add it. Add the store upsert/write seam to the
  re-embed migration (`iterate()` is read-only). Avoid 768-dim+ models. Only
  after D3+D1 measured and short of target.
- **D2 (query_expansion):** implement the reserved flag, **rule-based first**
  (zero token/latency), reuse `_rrf_fuse` for multi-query fusion. LLM-based
  expander last — it introduces the first LLM call inside retrieval (threads an
  LLMBackend through the signature, +100–300 tokens, +300–500ms). Ship only if
  D1+D3+rule-based plateau below target.
- **Serialize:** D1/D2/D3/D4 all edit RetrievalConfig + `_retrieve_inner` +
  DEFAULT_CONFIG — apply serially, never parallel.

### E · Memory-performance metrics surface (M6) — BUILD (extend fabri report)
Add one `## memory health` section to the existing report (reject a dedicated
`fabri memory-health` command). v1 headline set = **reuse-rate (already flowing)
+ guidelines-in-store + strategic-share% + median-entry-age**.
- **SystemExit fix (E2):** the plan reused `_open_store`, which `sys.exit(1)`s on
  an unreachable backend — `SystemExit` is a `BaseException`, so
  `except Exception` won't catch it, killing the (today fully-offline) report.
  **Call `build_memory_store(config['memory'])` directly** (already imported
  cli.py:21) inside `try/except Exception`, log-and-skip on failure leaving
  memory fields None.
- **compute:** `compute_memory_health(store)` — one `iterate()` pass +
  `count(kind=...)`, median age from `created_at`; new AggregateReport fields;
  consider a count()-only fast-path at 100k+ entries.
- **render:** add the section across md/json/html; relocate the loose reuse-rate
  line into it but **keep `avg_reuse_rate` at json top-level for back-compat**.
- **Phase 2:** reuse-rate trend sparkline (E5 — `svg_trendline` hardcodes
  `$%.4f` axis labels; parametrize the formatter before feeding it a percent
  series); strategy/latency rollup (E6 — hard-blocked on unit A's frozen event
  shape). **Later:** B exporter maps E's aggregates to root-span attributes (E7).

## 3. Global dependency-ordered build sequence

**Wave 0 — Foundations** (parallel across units, serial within a file)
- `A1` Add `EventType.RETRIEVAL` — one enum member.
- `A2` Thread `session_id` through `retrieve_context_with_meta` → `_retrieve_inner` → agent.py callsite.
- `C1` Labeled fixture `tests/fixtures/retrieval_eval.json` (text-unique-per-kind).
- `C2` Pure IR metrics module (recall@k / MRR / precision@k).
- `C5` CI install → `.[dev,sqlite]`.
- `B1` Decision card: OTel backbone. `B5` `fabri[otel]` extra + lazy-import scaffold.
- `E1` Route decision: memory-health section in fabri report.

**Wave 1 — Emit / measure / thread**
- `A3` Summary `retrieval` event (dense-path crash fix; store_count bind; inline volatile captures).
- `C3` Eval runner: fixture → tmp SqliteStore → `_retrieve_inner` → metrics (dup-id assert, deterministic tiebreak).
- `B4` `observability` config block + env overrides + run_config threading (needs B5).
- `E2` `compute_memory_health` + best-effort `build_memory_store` open (SystemExit fix); locks the 4-metric set.

**Wave 2 — Complete the core surfaces**
- `A4a` Lean per-candidate list for final merged set (original inclusion_reason + `mmr_survived`).
- `A5` Zero-overhead guard. `A6` Freeze the flat, JSON-serializable event contract for E/B.
- `C4` pytest gate with measured-baseline-minus-margin floor + deterministic sort (needs C3, C5).
- `B2` Post-hoc `export_trace` driver + agent.py end-of-run fire + `fabri traces export` CLI (needs B1, B4, B5).
- `E4` Render `## memory health` across md/json/html; relocate reuse-rate (keep json top-level).

**Wave 3 — Mapping, comparison tooling, tests**
- `A7` Net-new tests (MagicMock store + monkeypatch embed + FABRI_HOME isolation; dense + None cases).
- `B3` Event→span mapping incl. best-effort sub-agent cross-trace nesting (needs B2).
- `B6` Langfuse-as-OTLP-target recipe (config-only).
- `C6` Standalone strategy-comparison CLI (`python -m fabri.benchmarks.retrieval_eval`) — the D before/after tool.

**Wave 4 — Quality upgrades, gated on C** (serial; each measured before default-flip)
- `D3` Stage hybrid+mmr in benchmark.yaml; measure via C6; flip defaults only on positive delta + backend capability.
- `D1` Cross-encoder reranker over base-results portion (guaranteed slots pinned); measure delta.
- Then `D4` (embedding upgrade, only on plateau) → `D2` (query_expansion, last).

**Wave 5 — Phase-2 / later enrichment**
- `A4b` Full per-candidate provenance behind `memory.debug_retrieval`.
- `B9` Live inline span tap. `E5` reuse-rate trend (svg_trendline formatter fix). `E6` strategy/latency rollup (consumes A's event). `E7` B↔E span-attribute contract.

**Critical dependency chain:**
`A1→A2→A3→A6` (event contract) **and** `C1/C2/C5→C3→C4` (gate) **and** `C3→C6`
(comparison tool) **converge as the joint prerequisite for** `D3→D1→D4→D2`. In
parallel, `B1→B5→B4→B2→B3` builds the export pipe (auto-consumes A's event), and
`E1→E2→E4` builds the metrics surface; `E6` and B's retrieval-span attrs depend
on A's frozen `A6` contract.

## 4. Full decision table

| id | name | unit | verdict | effort | wave | depends-on | one-line HOW |
|---|---|---|---|---|---|---|---|
| A1 | EventType.RETRIEVAL | A | in_v1 | S | 0 | — | Add one grep-able enum member to events.py |
| A2 | Thread session_id | A | in_v1 | S | 0 | — | Optional `session_id=None` param through wrapper→_retrieve_inner→agent callsite |
| A3 | Summary retrieval event | A | in_v1 | M | 1 | A1,A2 | Build one guarded dict of config+pools+ranges+drop-counts; pre-init sparse_results to avoid dense-path NameError |
| A4a | Lean per-candidate list | A | in_v1 | M | 2 | A3 | Tag final-merged entries id/kind/score/inclusion_reason; add mmr_survived bool |
| A5 | Zero-overhead guard | A | in_v1 | S | 2 | A2,A3 | Dict/list build under `if session_id`; only ns-level scalar captures inline |
| A6 | Surfacing contract | A | in_v1 | S | 2 | A3 | Freeze flat JSON field names for E rollup + B span attrs |
| A7 | Tests | A | in_v1 | S | 3 | A3,A4a | Net-new MagicMock store + monkeypatch embed + FABRI_HOME isolation; dense + None cases |
| A4b | Full provenance (debug flag) | A | phase_2 | L | 5 | A3,A4a | dense/sparse scores + dropped records behind memory.debug_retrieval |
| B1 | OTel backbone decision | B | in_v1 | S | 0 | — | Own events, OTel export; reject bespoke + Langfuse-SDK |
| B5 | fabri[otel] extra + lazy import | B | in_v1 | S | 0 | B1 | Optional extra; import opentelemetry inside export_trace only |
| B4 | Config/env surface | B | in_v1 | S | 1 | B1,B5 | observability block + FABRI_OTLP_* env, threaded via run_config |
| B2 | Post-hoc export_trace driver | B | in_v1 | M | 2 | B1,B4,B5 | read_trace→span tree→OTLP flush at run end; `fabri traces export` CLI |
| B3 | Event→span mapping | B | in_v1 | M | 3 | B2 | run=root/step/tool spans; sub-agent nesting best-effort from spawn result payload |
| B6 | Langfuse as OTLP target | B | in_v1 | S | 3 | B3,B4,B5 | Config-only recipe: Langfuse OTLP URL + auth headers |
| B9 | Live inline span tap | B | phase_2 | L | 5 | B3 | Tap log_event, hold open spans, incremental flush |
| B7 | LangGraph | B | cut | S | — | — | Cut: DAG runtime, irrelevant to ReAct engine |
| B8 | Direct Langfuse SDK | B | cut | S | — | — | Cut: vendor lock-in; OTLP path covers it |
| B10 | Bespoke external backbone | B | cut | S | — | — | Cut: reinvents OTLP; richer in-repo work → A/E |
| C1 | Labeled fixture | C | in_v1 | M | 0 | — | JSON corpus+queries, binary relevance, text-unique-per-kind |
| C2 | IR metric functions | C | in_v1 | S | 0 | — | Pure recall@k/MRR/precision@k on id lists, stdlib only |
| C5 | CI install .[dev,sqlite] | C | in_v1 | S | 0 | — | Add sqlite extra so eval store instantiates (bm25 not needed for FTS5) |
| C3 | Eval harness core | C | in_v1 | M | 1 | C1,C2 | Fixture→tmp SqliteStore→_retrieve_inner→metrics; dup-id assert + deterministic tiebreak |
| C4 | CI gate | C | in_v1 | S | 2 | C3,C5 | pytest asserts dense recall@5/MRR ≥ measured-baseline−margin |
| C6 | Strategy-comparison CLI | C | in_v1 | M | 3 | C3 | `python -m` head-to-head recall/MRR table (D before/after tool) |
| C7 | Fold into fabri report | C | later | M | 5 | C6 | Seam to E; don't render inside C |
| C8 | Auto-gen/graded/nDCG | C | cut | L | — | C1 | Cut: reintroduces API cost + labeling burden |
| D3 | Flip default → hybrid+mmr | D | phase_2 | S | 4 | C | Stage in benchmark.yaml; flip defaults only on +delta + backend capability |
| D1 | Cross-encoder reranker | D | phase_2 | M | 4 | C | Rerank base-results portion (pin guaranteed slots); opt-in flag, no new dep |
| D4 | Embedding upgrade (bge-small) | D | later | L | 4 | C | Config-ize model into existing model_version field; re-embed migration (upsert) |
| D2 | query_expansion | D | later | M | 4 | C | Implement reserved flag rule-based first; reuse _rrf_fuse; LLM last |
| E1 | Route: report section | E | in_v1 | S | 0 | — | Add `## memory health` section, reject dedicated command |
| E2 | Store composition + age | E | in_v1 | M | 1 | E1 | build_memory_store (not _open_store) in try/except; iterate+count; locks 4-metric set |
| E3 | Headline metric set | E | in_v1 | S | 1 | E2 | Reuse-rate + total + strategic% + median age (folded into E2) |
| E4 | Render md/json/html | E | in_v1 | M | 2 | E1,E2 | Section across triple; relocate reuse-rate, keep json top-level |
| E5 | Reuse-rate trend | E | phase_2 | S | 5 | E4 | Sparkline; parametrize svg_trendline formatter for percent |
| E6 | Strategy/latency rollup | E | phase_2 | M | 5 | E4 | Parse A's retrieval events in summarize_session (needs A6 shape) |
| E7 | B exporter contract | E | later | S | 5 | E4 | B maps E aggregates to root-span attrs; stable field names |
| E8 | Dedicated CLI command | E | cut | S | — | — | Cut: duplicates pipeline, hides signal |

## 5. Resolved blockers & conflicts

1. **A3 dense-path crash (major):** `sparse_results` bound only inside the hybrid/sparse branch → `NameError` on the default dense path. Pre-init `sparse_results=[]`, `sparse_available=False`, `sparse_fallback=False` before the strategy branch; guard all sparse event fields; add a dense-strategy test to A7.
2. **A3 double `store.count()` (minor):** bind once (retrieval.py:379), branch on the variable.
3. **A5 zero-overhead overstatement (minor):** `embedding_ms` + `mmr_pool_before` scalar captures run inline regardless of session_id (ns-level); only the dict/list is guarded. Reword the guarantee.
4. **A4a inclusion_reason clobbered by MMR + MMR score semantics (minor):** keep original reason, add `mmr_survived`; document that `final_score` under MMR is marginal (can be negative), not similarity — E/B must not cross-compare.
5. **A7 phantom test harness + model download (major):** A7 is net-new — MagicMock store + monkeypatch `retrieval.embed` (no 44MB download) + `FABRI_HOME` tmp isolation (no such fixture exists). Includes dense + `session_id=None` cases.
6. **B2 reversed/missing build edges (major):** reorder to B1→B5→B4→B2→B3; B2.deps=[B1,B4,B5], B5.deps=[B1].
7. **B3 nonexistent spawn-session-id field (major):** child sid parsed best-effort from a successful spawn tool_call result payload → `read_trace(child_sid)` recurse; crashed children (`COST_UNACCOUNTED`) → unlinkable leaf spans with a cost-unaccounted attr. v1 accepts silent incompleteness for failed spawns.
8. **C5 wrong dependency rationale (major):** SQLite eval uses native FTS5 — only `sqlite-vec` is the hard unblocker; CI install is `.[dev,sqlite]`. Without it the gate SKIPS via `importorskip`, it does not fall back to dense. `bm25` deferred to when the Qdrant backend is exercised.
9. **C3 duplicate-id ground-truth corruption (minor):** assert corpus text unique within kind; assert `store.count()==len(corpus)` post-upsert before querying.
10. **C4 nondeterministic ordering (minor):** secondary sort key (entry.id); gate floor at measured-baseline−0.05, never exact equality — protects the recently-restored green suite.
11. **C1 false tag-slot coverage (minor):** `_retrieve_inner` gets no tool_names so tag-slots never fire; drop the coverage claim for v1 (scope fixture to dense/hybrid/domain).
12. **D1 guaranteed-slot eviction (major):** rerank only the base_results/filler portion, keep tag-hit/success_pattern slots pinned; rerank-vs-MMR ordering added to the C-measured A/B.
13. **D3 silent dense fallback (major behavioral):** stage in benchmark.yaml only; gate the real default-flip on a positive C delta *and* backend-capability detection (stock Qdrant without rank-bm25 → hybrid degrades to dense).
14. **D4 model_version framing (minor):** thread runtime model name into the existing per-entry `model_version` field (schema.py:27); add the store upsert seam to the migration.
15. **E2 SystemExit kills report (major):** use `build_memory_store` directly inside `try/except Exception` (log-and-skip), not `_open_store` (which `sys.exit(1)`s); preserves today's fully-offline report.
16. **E5 svg_trendline hardcoded $ labels (minor):** parametrize with a value-formatter before rendering a percent series (phase 2).
17. **Single-file serialization conflicts:** `retrieval.py::_retrieve_inner` (A2/A3/A4a/A5 + D1/D2/D3), `observability/otel.py` (B2/B3/B5), `RetrievalConfig`+DEFAULT_CONFIG (D1/D2/D3/D4), `reports/aggregate.py`+`render.py` (E2/E4/E5/E6) are each single shared surfaces — apply strictly in dependency order, **never** fan out to parallel per-file agents.
18. **Test trace isolation:** the autouse conftest fixture isolates only Qdrant collections, not trace files. All A7 tests set `FABRI_HOME` via monkeypatch to a tmp dir before emitting.

## 6. Open questions for the human

1. **OTLP transport default:** ship gRPC (port 4317) as the v1 default? Langfuse's OTLP ingest is HTTP/protobuf — support both in v1 (pick by endpoint scheme) or gRPC-only first, HTTP as a fast-follow? (Affects the B5 extra and B6 recipe.)
2. **C4 gate strictness:** confirm the floor = measured dense baseline − 0.05. Hard CI *failure* on regression, or warning-only (non-blocking) for the first N merges while the fixture proves representative?
3. **D3 default-flip authority:** once C shows a positive hybrid+mmr delta, flip the *global* default (+30–80ms for every user) or only a backend-capability-gated flip (hybrid only when sqlite-FTS5 or qdrant+bm25 detected, dense otherwise)?
4. **Cross-encoder vs guaranteed-slots (D1):** may cross-encoder relevance *supersede* tag-hit/success_pattern guaranteed slots, or must those always survive reranking? Default committed = slots pinned; confirm.
5. **Fixture authorship (C1):** who hand-labels the 20–30 query→relevant-guideline set, against what corpus, and does a second reviewer sign off on the relevance labels before the gate goes live?
6. **B2 crash-flush limitation:** post-hoc export skips a run that hard-crashes before end. Acceptable for v1 (matches `fabri report`'s post-hoc limit), or is live streaming (B9) needed sooner?

## Key file anchors

`src/fabri/orchestrator/retrieval.py` (RetrievalConfig ~34-66, `_retrieve_inner`
~367-508), `src/fabri/orchestrator/events.py` (EventType ~17-63),
`src/fabri/orchestrator/traces.py` (`log_event` ~25),
`src/fabri/core/agent.py` (retrieval callsite ~182, end-of-run ~798-818),
`src/fabri/config.py` (DEFAULT_CONFIG), `src/fabri/core/run_config.py` (~97),
`src/fabri/memory/embeddings.py` (MODEL_NAME ~21-22),
`src/fabri/memory/schema.py` (model_version ~27),
`src/fabri/reports/{aggregate,render,chart}.py`,
`src/fabri/cli.py` (~21, ~92, ~788), `pyproject.toml` optional-deps,
`.github/workflows/ci.yml`, `configs/benchmark.yaml`.
