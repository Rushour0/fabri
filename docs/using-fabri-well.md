# Using fabri well — the operational loop that makes memory compound

Fabri's value shows up **across runs**, not within one. A single run is just an agent
with tools; the payoff is that each run *teaches the next one*. This guide is about
operating fabri so that learning actually accumulates and stays healthy.

It deliberately does **not** repeat material that already exists:

- **Setup / library embedding** → `README.md` §"Using it as a library"
- **Building an agent + config** → `docs/creating-an-agent.md`
- **Retrieval knobs (rrf_k, strategies, decay, …)** → `docs/retrieval-tuning.md`
- **How the pieces fit** → `docs/HOW_FABRI_WORKS.md`

What's below is the connective tissue those docs assume you already know.

---

## 1. The learning loop

The whole point of fabri is this cycle:

```
fabri run "task"        # agent executes; the run is traced and mines guidelines
   ↓
memory grows            # tactical/strategic guidelines + (optional) a postmortem
   ↓
fabri run "next task"   # retrieval injects the relevant learnings into the prompt
   ↓
better execution → more/better guidelines → …
```

A run learns **on its own** — it synthesizes guidelines from the run's own tool
outcomes and, when `memory.record_postmortems` is on, writes a whole-run postmortem.
You then have two explicit levers to grow memory further:

| Command | Use it to |
|---|---|
| `fabri ingest-traces <session-id>` | **Re-mine one of fabri's own past runs** into guidelines (e.g. after tuning synthesis, or to backfill runs made before memory was enabled). |
| `fabri ingest <log> --adapter auto` | **The Improver** — mine *external* logs you already have (jsonl / regex / otel / openai / plugin adapters) into memory. Deterministic and **$0 by default**; add `--synthesize` for LLM compression. Use `--dry-run` to parse+count without writing, `--list-adapters` to see options. See README §"The Improver". |

**Optimal habit:** let memory accumulate. Don't wipe the store between related sessions
— a cold store short-circuits retrieval entirely (no model load, cheap), but it also
means zero learning has compounded yet. The store gets *more* useful with age, and
promotion (below) only fires once a lesson recurs across ≥3 sessions.

## 2. Picking a backend

Set `memory.backend` in your agent config. The choice is a real trade-off:

| Backend | Reach for it when | Watch out for |
|---|---|---|
| **`sqlite`** (sqlite-vec + native FTS5) | Default for most agents. Self-contained, no service, cheapest, and hybrid retrieval works out of the box (FTS5 provides BM25). | Single-host; not for many concurrent writers across machines. |
| **`qdrant`** | You need a shared/remote vector service or scale beyond one host. | **Hybrid silently degrades to dense** unless the `fabri[bm25]` extra is installed — Qdrant has no built-in BM25, so fabri falls back to client-side `rank_bm25` only if present. Without it, `strategy: hybrid` runs as `dense` and you won't be told at the prompt level. |

**How to catch the silent degrade:** every retrieval emits a `retrieval` trace event with
a `sparse_fallback` field. If you set `hybrid` on Qdrant and see `sparse_fallback: true`,
you're running dense — install `fabri[bm25]` or switch to the sqlite backend. (See §4.)

**Cheapest self-contained path:** `sqlite` backend + `retrieval_strategy: dense` — no
Qdrant, no BM25 extra, no fusion. Only give up hybrid if you've measured it doesn't help
your corpus (see §5).

## 3. Reading what memory actually did

Don't guess what the store holds or why a guideline surfaced — inspect it:

| Command | Shows |
|---|---|
| `fabri memory show` | Human-readable listing of stored guidelines. |
| `fabri memory list` | JSONL listing (pipeable into `jq`). |
| `fabri memory diff <sid-a> <sid-b>` | What memory changed between two sessions — the audit trail for "did this run actually learn anything?" |
| `fabri inspect-memory ["query"]` | Inspect the store, optionally running a query against it to see what *would* be retrieved. |
| `fabri traces show <sid>` / `traces tail <sid>` | The run's JSONL trace, including the `retrieval` event. |

### The `retrieval` trace event is your debugger

Every retrieval logs one structured event (trace-only, **zero prompt cost**) recording
the strategy, dense/sparse pool sizes, whether BM25 fired or fell back, per-candidate
`inclusion_reason`, and MMR state. It's the fastest way to answer:

- *"Is my hybrid secretly running as dense?"* → look for `sparse_fallback: true`.
- *"Why did this guideline get injected?"* → `inclusion_reason` (`base` / `tag_hit` /
  `success_pattern`), plus `mmr_survived` if MMR is on.
- *"Why did the obviously-relevant one NOT show up?"* → check pool sizes and ranks; it
  may be a genuine relevance miss, not a config bug.

Read it straight from the trace JSONL, or export it to an OTLP backend once the OTel
export path is wired (see `docs/design/external-memory-patterns.md` §R4).

## 4. Measure before you tune (the golden rule)

**Never flip a retrieval default blind.** Fabri ships an offline eval precisely so a
retrieval change moves a *number*, not a vibe:

```bash
python -m fabri.benchmarks.retrieval_eval                          # markdown table, all strategies
python -m fabri.benchmarks.retrieval_eval --strategies dense,hybrid --json
python -m fabri.benchmarks.retrieval_eval --fixture my_corpus.json --top-k 8
```

To tune on **your** data, copy `tests/fixtures/retrieval_eval.json`, replace `corpus`
(your guidelines) and `queries` (tasks tagged with the guideline label(s) that *should*
surface), and compare strategies / `rrf_k` on your own numbers. Keep guideline text
**unique within a `kind`** — identical text collapses under the store's id hash
(id = `sha256(namespace::text)`), silently corrupting both the store and any eval fixture.

The CI gate (`tests/test_retrieval_eval_gate.py`) locks shipped defaults to
`measured − 0.05`, so a regression can't slip in unnoticed. All knob-by-knob guidance
lives in `docs/retrieval-tuning.md`.

## 5. Memory hygiene — what happens automatically, and what you can do

Fabri keeps the store healthy on its own, at ingest time:

- **Dedup:** a near-duplicate (cosine ≥ 0.85) doesn't create a new entry — it increments
  `hit_count` and merges `session_ids`/`tools` (`memory/pruning.py`).
- **Promotion:** a `tactical` guideline seen across ≥3 distinct sessions is promoted to
  `strategic` (battle-tested, eviction-protected).
- **Eviction:** when the store exceeds `max_entries`, low-value entries are dropped by
  `hit_count × temporal_decay(age)`; `strategic` is protected until nothing else remains.
  With `eviction_strategy: summarize`, evicted groups are LLM-compressed before deletion.
- **Postmortems:** with `record_postmortems: true`, each run writes a whole-run summary.

What you should do periodically:

- **Watch memory health.** `fabri report` surfaces reuse-rate, store size, strategic
  share, and median entry age. Falling reuse-rate or a ballooning tactical share is the
  signal to consolidate.
- **Turn on `temporal_decay`** if your domain drifts (APIs change, last month's lesson is
  stale). Leave it off for timeless guidelines. (`docs/retrieval-tuning.md`.)
- **A deliberate consolidation pass** (dedup sweep + staged promotion + staleness prune +
  contradiction detection) is proposed as `fabri consolidate` in
  `docs/design/external-memory-patterns.md` §R2 — not built yet, but that's where this
  hygiene becomes an explicit, reviewable command rather than a side effect.

---

## Do / don't

**Do**

- Let memory accumulate across related sessions — learning compounds; promotion needs recurrence.
- Default to the `sqlite` backend unless you specifically need a shared vector service.
- Check `sparse_fallback` in the `retrieval` event whenever you run `hybrid` on Qdrant.
- Measure with the eval CLI before changing any retrieval knob; tune on a fixture built
  from *your* corpus.
- Keep guideline text unique within a `kind`.

**Don't**

- Don't hand-flip retrieval defaults on a hunch — move a number first.
- Don't run `hybrid` on Qdrant without `fabri[bm25]` and assume you're getting fusion.
- Don't wipe the store to "start clean" between related runs — you're discarding the exact
  thing fabri exists to build.
- Don't paste identical guideline text under the same kind — the id hash will collapse them.

## See also

- `docs/design/external-memory-patterns.md` — how Hermes Agent and OpenClaw handle memory,
  and the four recommendations (Protocol, consolidation pass, core-digest tier, OTel wiring).
- `docs/retrieval-tuning.md` — the full knob reference.
- `docs/design/memory-observability-plan.md` — the M3–M6 / X1 roadmap these build on.
