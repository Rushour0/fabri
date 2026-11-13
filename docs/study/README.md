# fabri study docs

A self-contained study pack covering everything built in fabri from 2026-07-07 through
2026-07-24 (306 commits, ~89 PRs, releases 0.13 → 0.20.0). Written for the project owner to
(re)learn the whole system — what was built, why, how it works, and what the evidence
actually says. Local reading material only; nothing here is published or committed by
default.

## Reading order

1. **[architecture.md](architecture.md)** — how fabri works: the core agent engine,
   agencies/companies, tools, LLM layer, COGS tracking, and the serve/SSE service. Start
   here for the mental model.
2. **[memory-system.md](memory-system.md)** — the core bet: self-improving agents via
   persistent memory. Guideline mining, retrieval stack, the dedup fix, ActionMemory,
   tiering, and convention mining v1. The longest and most important doc.
3. **[benchmarks-and-evals.md](benchmarks-and-evals.md)** — how the memory bet was tested:
   the harness, the memory-vs-control studies, evaluator honesty fixes, headroom smokes, and
   the statistical rigor work.
4. **[key-decisions-and-lessons.md](key-decisions-and-lessons.md)** — the decision log: every
   strategic call with rationale, the honest results ledger (what's proven / falsified /
   open), the ten bugs worth studying, and process lessons.
5. **[timeline.md](timeline.md)** — the chronological narrative: what shipped in each
   release era, with PR numbers.
6. **[studio-and-ux.md](studio-and-ux.md)** — Fabri Studio: the conversational UI, Company
   view, COGS panel, fleet mode, HITL, and the service layer behind it.
7. **[distribution-and-ecosystem.md](distribution-and-ecosystem.md)** — everything around
   the engine: CLI/scaffolder, rosters catalog, connectors, the repo agent, hosting,
   provider support, and the paperclip-ai GTM positioning.

## The one-paragraph summary

fabri is an open-source engine for running "companies" of LLM agents (manager → specialist
crews) with per-run cost accounting (COGS) and a persistent memory loop that mines lessons
from run traces and feeds them back into future runs. The mechanical loop —
mine → store → dedup → retrieve → promote → reuse — is proven live. The value claim —
that memory makes runs measurably better or cheaper — is **not yet proven**: honest
benchmarking repeatedly found null effects, and each round of diagnosis uncovered a deeper
engine or instrument bug (dead BM25, paraphrase-drift dedup, unmined sub-agent traces,
truncating token caps, evaluator artifacts). That diagnostic grind drove the project's main
pivot: from injecting guideline *text* into prompts toward engine-side, fail-closed
**memory-as-action** and **convention mining**, where the model at most selects a branch and
the engine does the rest. As of 2026-07-24 the convention *supply* pipeline works end-to-end;
convention *application* at the holdout is the last open gap.
