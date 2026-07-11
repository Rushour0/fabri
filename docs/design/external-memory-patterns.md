# External memory patterns (Hermes Agent, OpenClaw) → what fabri should borrow

> **Status:** analysis + recommendations, not yet built (2026-07-11). Docs-only.
> **Companion:** extends `docs/design/memory-observability-plan.md` (the M3–M6 / X1
> roadmap). Nothing here restates that plan; it adds four net-new recommendations
> (R1–R4) that sit on top of it. Usage-facing guidance lives in
> `docs/using-fabri-well.md`.

## Why this doc exists

We surveyed how two open-source agent-memory projects handle memory and asked what
fabri should adopt. The survey findings below come from each project's **public docs
and README** (fetched, not read from raw source), so external claims are phrased as
"per their docs" and should be treated as such. Every *fabri* claim is anchored to a
file in this repo and was spot-checked against the code on 2026-07-11.

### The framing that governs everything

Fabri and these two projects are solving **different memory problems**, and conflating
them would import the wrong ideas.

- **Fabri is agent-*learning* memory (procedural).** A "memory" in fabri is a mined
  *guideline* — `kind ∈ {tactical, strategic, success_pattern, postmortem}` — distilled
  from prior runs' tool outputs so future runs execute better
  (`src/fabri/memory/schema.py:21`, `:27-29`). It answers *"what has this agent learned
  about doing this kind of task?"*
- **Hermes Agent and OpenClaw are personal-assistant memory (declarative/episodic).**
  Their memories are user facts, preferences, decisions, and conversation logs. They
  answer *"what do I know about this user and our history?"*

Consequence: patterns about **storage, retrieval, consolidation, and observability**
transfer well (they're substrate-level). Patterns about **user profiles, "remember I
prefer X," and approval/commitment memory** do **not** — see "Explicitly not
recommended." The recommendations below are the subset that actually fits a procedural
learning store.

---

## 1. How Hermes Agent handles memory

*Source: `github.com/NousResearch/hermes-agent`, docs
`.../website/docs/user-guide/features/memory-providers.md`. Distinct from the Nous
Research "Hermes" LLM fine-tunes — this is a separate agent framework.*

- **No single store; a pluggable-provider system.** Ships file-based built-ins
  (`MEMORY.md`, `USER.md`) plus **8 swappable external providers, one active at a
  time**, selected via `hermes memory setup` (Honcho, OpenViking, Mem0, Hindsight,
  Holographic, RetainDB, ByteRover, Supermemory). Backends range across peer-conversation
  models, hierarchical filesystem stores, LLM-extracted deduped facts, knowledge graphs,
  local SQLite+FTS5, and hybrid vector+BM25+rerank stores.
- **Per-turn lifecycle:** inject provider context into the system prompt → non-blocking
  prefetch of relevant memories before the turn → sync the turn after the response →
  extract memories at session end → mirror built-in file writes to the provider.
- **Tiered progressive loading** (OpenViking): abstract (~100 tokens) → overview (~2k) →
  full document, so context is spent lazily.
- **Strict per-profile scoping** — local providers keyed under `$HERMES_HOME/`, cloud
  providers auto-derive profile-scoped project IDs.

**The transferable idea:** *the memory backend is an interface, not a hardcoded
implementation.* Everything else (extraction cadence, provider zoo) is assistant-shaped.

## 2. How OpenClaw handles memory

*Source: `github.com/openclaw/openclaw`, docs `docs.openclaw.ai/concepts/memory`.*

- **Human-readable tiers on disk:** `MEMORY.md` (durable long-term, **injected at
  session start**, auto-truncated to a budget) / `memory/YYYY-MM-DD.md` (short-term
  working notes, **searchable but not always injected**) / `DREAMS.md` (consolidation
  output for human review). Design philosophy: "the model only remembers what gets saved
  to disk; there is no hidden state."
- **"Dreaming" — a reflection/consolidation loop.** A background pass that scores
  short-term candidates against thresholds, **promotes** qualifying items from daily
  notes into `MEMORY.md`, **prunes stale** long-term entries, and writes a
  human-reviewable summary. "Grounded Backfill" replays history into the short-term
  store for staged review **without auto-promotion** — a human-in-the-loop gate.
- **Hybrid retrieval:** semantic vector search + keyword matching for exact terms (IDs,
  code symbols); tools `memory_search` / `memory_get`. Pluggable backends here too
  (Builtin SQLite, QMD, Honcho, LanceDB).
- **Action-sensitive / commitment memory:** a distinct category for approvals,
  constraints, handoffs, expiry — "what changes future behavior."
- **Memory Wiki (optional):** a provenance layer with claims tracking, **contradiction
  detection**, and freshness metrics.

**The transferable ideas:** *(a)* an always-injected small long-term tier distinct from
the searchable log; *(b)* a deliberate offline consolidation pass with a human-review
gate and contradiction detection.

## 3. What both converge on

Independently, both land on the same four-part shape:

1. a **small always-injected** long-term summary,
2. a **larger searchable** short-term/episodic log,
3. a **periodic reflection** process promoting short → long and pruning stale, and
4. a **swappable backend** behind a common interface.

(Honcho shows up as a shared pluggable provider in *both* projects — evidence the
interface boundary is the real product surface.)

---

## 4. Mapping to fabri: what we already have vs. the gap

| Convergent pattern | Fabri today | Anchor | Verdict |
|---|---|---|---|
| Hybrid semantic + keyword retrieval | ✅ **Ahead** — RRF fusion of dense + BM25/FTS5, `hybrid` default, MMR option, `sparse_fallback` trace signal | `orchestrator/retrieval.py:71`, `:461`, `:486` | Keep |
| Offline quality measurement | ✅ **Ahead** — deterministic recall@k/MRR eval gate in CI (M4) | `benchmarks/retrieval_eval/`, `tests/test_retrieval_eval_gate.py` | Keep |
| Memory-health metrics | ✅ Planned (M6) — reuse-rate, store size, strategic-share, median age in `fabri report` | `memory-observability-plan.md` §E | Keep |
| Inline consolidation (dedup, promote, evict, summarize) | ✅ Partial — done **inline at ingest**, not as a deliberate pass | `memory/pruning.py:235` (`ingest_guideline`), `:157` (`_evict_if_needed`), `:46` (`_summarize_and_evict`) | **→ R2** |
| Swappable backend behind an interface | ⚠️ **Gap** — Qdrant + sqlite share a method surface but it's **duck-typed, no Protocol** | `memory/store.py`, `memory/embedded_store.py`, `runtime.py::build_memory_store` | **→ R1** |
| Always-injected long-term tier | ⚠️ **Gap** — everything reaches the prompt via retrieval; no always-on strategic digest | `orchestrator/retrieval.py` `_retrieve_inner` | **→ R3** |
| External trace export / observability | ⚠️ **Landed but unwired** — `observability/otel.py` exists; **zero callsites** outside that dir, no `traces export` verb | `observability/otel.py`, `cli.py` (only `traces show/tail/list`) | **→ R4** |
| Contradiction detection / provenance freshness | ⚠️ Gap (provenance fields exist; no contradiction pass) | `schema.py` (`session_ids`, `created_at`, `hit_count`) | **→ R2 sub-piece** |

Fabri is genuinely **ahead** on retrieval and measurement — the survey does not push us
to change those. The four gaps map cleanly onto R1–R4.

---

## 5. Recommendations (ordered by value-over-risk)

Each is a design sketch, not an implementation. All follow fabri's golden rule: **never
flip a retrieval default blind** — anything touching the injected set must move a number
on `python -m fabri.benchmarks.retrieval_eval` first.

### R1 · Formalize a `MemoryStore` Protocol — *lowest risk, do first*

**What.** Promote the surface both stores already implement into a
`typing.Protocol` (optionally `@runtime_checkable`), mirroring the existing `LLMBackend`
(`core/llm.py:110`) and `Adapter` (`ingest/adapters/base.py:43`, which uses
`@runtime_checkable`). The shared surface is real and already identical:
`upsert / get / query / query_by_vector / find_similar / delete / count / iterate`
(both stores), with `query_bm25` as an **optional** capability present only on
`SqliteMemoryStore` (`embedded_store.py:239`) — model it as a separate optional Protocol
or a `hasattr`-probed capability, exactly as retrieval already treats it.

**Why.** Validated by Hermes' 8-provider model and OpenClaw's pluggable backends: the
interface *is* the extension point. This is also already the intended shape — the
Weaviate backend is deferred behind "a future MemoryStore Protocol." Formalizing it
turns retrieval's `hasattr(store, 'query_bm25')` probe into a typed capability and gives
new backends a checklist instead of a duck-typing guess.

**Fabri fit / effort / risk.** Pure typing + a small refactor of `build_memory_store`'s
return annotation; **no behavior change**, so no eval impact. Lowest-risk item here.

**Roadmap.** Net-new; unblocks the deferred Weaviate backend. No M-track dependency.

### R2 · An offline consolidation / "dream" pass (`fabri consolidate`)

**What.** Elevate fabri's *inline* hygiene into an explicit, auditable batch command.
The machinery mostly exists — this is orchestration + one new sub-piece:

- **Dedup** — already implemented (cosine ≥ `SIMILARITY_THRESHOLD = 0.85`,
  `pruning.py:15`); run it as a cross-corpus sweep, not just at ingest.
- **Promotion** — already implemented (tactical → strategic after
  `PROMOTION_THRESHOLD_SESSIONS = 3`, `pruning.py:16`); expose it as a staged step with
  an **optional human-review gate** (OpenClaw's "Grounded Backfill": propose promotions,
  apply only on approval).
- **Staleness pruning** — reuse eviction's temporal scoring (`_evict_if_needed`,
  `pruning.py:157`) and the `temporal_half_life_days` knob.
- **Summarize** — reuse the MemGPT-style `_summarize_and_evict` (`pruning.py:46`).
- **Contradiction detection — the one genuinely new sub-piece.** Flag guideline pairs
  that are semantically similar but carry opposite `outcome` (`schema.py:29`) or give
  conflicting advice, and surface them for review. This is OpenClaw's "Memory Wiki"
  contradiction idea, scoped to procedural guidelines.

**Why.** Both surveyed projects treat consolidation as a deliberate, reviewable pass
rather than a side effect. Fabri's inline approach works but is invisible and
un-auditable; a `fabri consolidate` command makes memory hygiene a first-class,
inspectable operation.

**Fabri fit / effort / risk.** Medium. Reuses existing functions; the human-review gate
and contradiction detector are the new code. Risk is low because it's an explicit,
opt-in command, not an automatic behavior change — but any step that *deletes* or
*rewrites* guidelines must be dry-run-able and diffable (pair it with existing
`fabri memory diff`).

**Roadmap.** Net-new; complements M6 memory-health (health tells you *when* to
consolidate; this *does* it).

### R3 · An always-injected "core digest" tier — *eval-gated, opt-in*

**What.** Inject a small, bounded set of top `strategic` guidelines into **every** run
regardless of retrieval match — fabri's analog of OpenClaw's always-loaded `MEMORY.md`.
Implemented as reserved slots in `_retrieve_inner` (like the existing back-loaded
`success_pattern` slots), guarded behind an opt-in flag with a hard size cap so it can't
crowd out relevance-matched results.

**Why.** Some strategic learnings are universally applicable and shouldn't depend on a
query keyword-matching them. This is the "small always-injected long-term tier" both
projects rely on.

**Fabri fit / effort / risk.** Medium, and **the highest-risk item on retrieval quality**
— it consumes prompt slots and tokens unconditionally. This is exactly the class of
change fabri's golden rule exists for: stage it behind a flag in `configs/benchmark.yaml`,
measure dense/hybrid recall@k with and without it on the eval fixture, and only consider a
default-flip on a positive, backend-aware delta. Mirror the D3 discipline in
`memory-observability-plan.md`.

**Roadmap.** Extends the M5 (D-track) "opt-in flag, gated on a measured C delta" pattern.

### R4 · Finish the OTel observability wiring (X1 / unit B last mile)

**What.** `observability/otel.py::export_trace` + `OtelConfig` **exist but are wired to
nothing** — confirmed: no references outside `observability/`, and `cli.py` exposes only
`traces show / tail / list`, no `export`. Finish exactly what `memory-observability-plan.md`
§B specs: the `fabri traces export <session_id>` CLI verb (B2), the end-of-run auto-fire
guarded by `if otel_cfg.endpoint` (B2, `core/agent.py` end-of-run), event→span mapping
incl. best-effort sub-agent nesting (B3), and the Langfuse-as-OTLP recipe (B6). The
`observability:` config block and the optional `fabri[otel]` extra are already scoped.

**Why.** This isn't new design — it's the undone last mile of an already-decided unit.
Both surveyed projects expose memory/agent observability (`openclaw memory status`,
provider config surfaces); fabri's version is built but dark. Wiring it lets any OTLP
backend (Langfuse, Honeycomb, Grafana Tempo, Jaeger) see the trace spine, including the
M3 `retrieval` event, for free.

**Fabri fit / effort / risk.** Small–medium, low risk (off by default, requires an
endpoint). Follow the corrected build order in §B: `B1 → B5 → B4 → B2 → B3 → B6`.

**Roadmap.** Directly completes X1 / unit B.

### Suggested sequence

`R1` (unblocks clean backend work, zero risk) → `R4` (finish an in-flight unit) → `R2`
(consolidation, reuses existing machinery) → `R3` (highest retrieval-quality risk, do
last and fully eval-gated).

---

## 6. Explicitly **not** recommended (and why)

These are load-bearing in an assistant but wrong for a procedural learning store:

- **User-profile files (`USER.md`) / "remember I prefer TypeScript."** Fabri memories are
  learned from *run outcomes*, not asserted by a user. There is no user-preference
  surface to model, and adding one would blur the procedural/declarative line the whole
  design rests on.
- **Approval / commitment memory with expiry.** Constraints, handoffs, and approvals are
  session-policy concerns, not durable cross-run learnings. Fabri already scopes
  per-session via traces; enforcement belongs in the run loop, not the guideline store.
- **Progressive tiered loading (abstract → overview → full).** Unnecessary: fabri
  deliberately caps guidelines at ~30 tokens (`memory/compress.py` `enforce_token_cap`,
  `DEFAULT_MAX_TOKENS`), so there is no large document to load lazily.
- **A provider zoo (8 backends).** R1 gives the *interface*; adding many backends is
  cost without demand. Qdrant + sqlite cover the deployment spectrum (service vs.
  self-contained); Weaviate is the one queued addition.

---

## Related

- `docs/optimization-methodologies.md` — the *transferable* optimization ideas
  from Hermes/OpenClaw, mapped to real fabri mechanisms and the runnable
  `examples/` that demonstrate each.
- `docs/using-fabri-well.md` — the operational loop that makes memory compound.

## Verification note

All fabri anchors verified against the working tree on 2026-07-11 (store method
surfaces, `pruning.py` constants, `schema.py` fields, retrieval strategy/fence, CLI
subparsers, and the absence of any `export_trace` callsite or `traces export` verb).
External behavior is attributed to each project's published docs, fetched via
summarization rather than raw source — treat those descriptions as "per their docs,"
not independently verified implementation fact.
