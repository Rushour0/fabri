# Tuning memory retrieval

Fabri retrieves guidelines from memory and injects the most relevant ones into
each run's prompt. The defaults are eval-backed and good for most stores — **if
you're just starting out, change nothing.** This guide is for when you want to
tune retrieval to your own corpus, and it shows the exact knobs, sane values,
and how to *measure* whether a change helped instead of guessing.

Every knob lives under the `memory:` block of your agent config (or the global
`DEFAULT_CONFIG`). All are optional; omitting one keeps its default.

> **Golden rule: never flip a retrieval default blind.** Fabri ships an offline
> eval (`python -m fabri.benchmarks.retrieval_eval`) precisely so retrieval
> changes move a *number*, not a vibe. Measure before and after.

---

## Start here: the default is `hybrid`, and why

```yaml
memory:
  retrieval_strategy: hybrid   # the default — you can delete this line
```

On fabri's labeled eval fixture (40 guidelines / 24 queries, top_k=5), `hybrid`
is the best strategy on **every** metric:

| strategy | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| dense | 0.583 | 0.688 | 0.792 | 0.790 |
| sparse | 0.500 | 0.792 | 0.875 | 0.772 |
| **hybrid (default)** | **0.583** | **0.896** | **0.938** | **0.844** |
| hybrid+mmr | 0.583 | 0.625 | 0.729 | 0.804 |

`hybrid` fuses dense (vector) and sparse (BM25) retrieval, and **degrades
gracefully to dense** wherever BM25 is unavailable (a Qdrant store without the
`fabri[bm25]` extra), so it is never worse than plain `dense`. That's why it's a
safe unconditional default.

---

## The four strategies — when to pick each

```yaml
memory:
  retrieval_strategy: dense   # | sparse | hybrid | hybrid+mmr
```

| strategy | what it does | reach for it when |
|---|---|---|
| `hybrid` | RRF fusion of vector + BM25 | **default.** Best all-round; keyword + semantic. |
| `dense` | vector similarity only | your queries are paraphrases with little keyword overlap, or you want the lowest-dependency path. |
| `sparse` | BM25 keyword only | your guidelines are keyword/identifier-heavy (tool names, error codes, API paths) and you don't want vector cost. |
| `hybrid+mmr` | hybrid, then diversify the final set | your store has many **near-duplicate** guidelines and the injected set keeps repeating the same point. MMR trades a little recall for diversity — measure first. |

---

## Knob reference (with recommended values)

### `rrf_k` — RRF fusion sharpness (hybrid only)

```yaml
memory:
  rrf_k: 20   # default
```

RRF scores each candidate `Σ 1/(rrf_k + rank)`. The classic web-scale default is
`60`, but fabri fuses **two short pools** (~2·top_k each), where `60` flattens
the rank term so much that "appears in both lists" outranks "is the single best
match." Retuning `60 → 20` lifted hybrid **recall@3 0.60 → 0.90** with recall@5
unchanged.

- **Lower (10–20):** sharper — the top hit of either signal keeps its rank.
  Best for small/medium stores.
- **Higher (40–60):** rewards agreement between signals over peak relevance.
  Only worth it for very large stores where both pools are long.

### `temporal_decay` / `temporal_half_life_days` — prefer recent guidelines

```yaml
memory:
  temporal_decay: true            # default false
  temporal_half_life_days: 30.0   # a 30-day-old entry is weighted ~0.5
```

Turn on when your domain **drifts** (APIs change, last month's lesson is stale).
Leave off when guidelines are timeless. Shorter half-life = more aggressive
recency bias.

### `importance_weight` — boost proven guidelines

```yaml
memory:
  importance_weight: 0.2   # default; 0 disables
```

Boosts entries by how often they've been retrieved plus a bonus for `strategic`
ones: `score *= 1 + importance_weight * importance`. Raise (→0.4) to lean harder
on battle-tested guidelines; set `0` for pure relevance.

### `domain_routing` — soft-boost same-domain guidelines

```yaml
memory:
  domain_routing: true   # default false
```

A zero-cost keyword classifier tags each query as code/planning/search/api/
generic and gives same-domain entries a 1.15× boost (never a hard filter). Turn
on when one agent spans clearly different domains and cross-domain guidelines
leak in.

### `mmr_lambda` — relevance vs diversity (hybrid+mmr only)

```yaml
memory:
  retrieval_strategy: hybrid+mmr
  mmr_lambda: 0.7   # 1.0 = pure relevance, 0.0 = pure diversity
```

Only applies to `hybrid+mmr`. Lower it toward 0.5 if the injected set is
redundant; keep ≥0.7 so relevance still dominates.

### `top_k` — how many guidelines get injected

```yaml
memory:
  top_k: 5   # default
```

More guidelines = more context (and tokens) per turn. Raising `top_k` trades
prompt cost for coverage; the recall@k numbers above are at `top_k=5`.

---

## About the `success_pattern` guarantee (no knob — just so you know)

Fabri always reserves up to `top_k // 2` slots for `success_pattern`
guidelines ("what worked last time"), so learned strategies surface even when a
query doesn't obviously match one. These slots are **back-loaded** — the most
relevant guideline always keeps rank 1, and success patterns fill reserved
*tail* slots. (Front-loading them, an earlier bug, sank recall@1 from 0.58 to
0.13.) You don't configure this; it's just why you may see a strategy guideline
in the injected set that wasn't the closest match.

---

## Recipes

**"My store is tiny (a fresh agent)."** Defaults are fine. Retrieval short-
circuits entirely on an empty store (no model load), so cold starts are cheap.

**"I want the cheapest / most self-contained path."** `dense` with the `sqlite`
backend — no Qdrant, no `fabri[bm25]`, no BM25 index.

**"My guidelines are full of tool names / error codes / paths."** `sparse` or
keep `hybrid` and lower `rrf_k` to ~10 so exact keyword hits rank first.

**"The same advice keeps repeating in the prompt."** `hybrid+mmr` with
`mmr_lambda: 0.6`. Measure — MMR can cost recall.

**"My domain changes fast."** `temporal_decay: true`,
`temporal_half_life_days: 14`.

**"Large store (100k+ entries), fusion feels muddy."** Try `rrf_k: 40` and
consider `domain_routing: true`.

---

## Measure your tuning

Run the head-to-head eval — it drives the **real** retrieval path, so the
numbers reflect production:

```bash
python -m fabri.benchmarks.retrieval_eval                       # markdown table
python -m fabri.benchmarks.retrieval_eval --strategies dense,hybrid --json
python -m fabri.benchmarks.retrieval_eval --fixture my_corpus.json --top-k 8
```

To tune on **your** data, copy `tests/fixtures/retrieval_eval.json` and replace
`corpus` (your guidelines) + `queries` (tasks tagged with the guideline
label(s) that *should* surface). Keep guideline text unique within a `kind`
(identical text collapses under the store's id hash). Then compare strategies /
`rrf_k` values on your own numbers.

The CI gate in `tests/test_retrieval_eval_gate.py` locks the shipped defaults to
`measured − 0.05` so a future change can't silently regress them.

## Debug what retrieval actually decided

Every retrieval emits one structured `retrieval` trace event (strategy, dense/
sparse pool sizes, whether BM25 fired or silently fell back to dense, per-
candidate `inclusion_reason`, MMR). It's trace-only (zero prompt cost). Read it
straight from the run's JSONL trace to see *why* a guideline was or wasn't
injected — the fastest way to tell "hybrid is secretly running as dense" (look
for `sparse_fallback: true`) from a genuine relevance miss.

See `docs/design/memory-observability-plan.md` for the event contract and
`BENCHMARKS.md` for the full eval findings.
