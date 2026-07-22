# Revenue Ops live failure curriculum smoke (2026-07-22)

- **Company:** Revenue Ops / `market_research_brief`
- **Fabri source:** working tree based on 0.19.2; implementation changed between episodes
- **Roster revision:** `fabri-rosters@1d83a77ad524955063233f4dddf13410c7224999` (roster worktree dirty; source roster was not modified)
- **Provider:** OpenAI only
- **Purpose:** engineering smoke for `fail → diagnose → remember → apply → retry`, not a causal memory-vs-control benchmark
- **Fault injection:** compiled researcher and writer roles were reduced from 2048 to 128 output tokens. The source roster stayed unchanged.

## Outcome

The company progressed from repeated truncation failures with no deliverable to a
`success_with_recovery` run that created a 632-word, source-cited
`deliverables/brief.md`. The successful episode automatically applied two scoped
ActionMemory changes before delegation:

- researcher: `max_tokens 128 → 256`
- writer: `max_tokens 512 → 1024`

The output cited `source/topic.md`, labeled unsupported claims as unknowns, compared
both candidate segments, and supplied measurable validation experiments.

## Episodes

Trace spend sums every root/child `usage.cost_usd` and `post_run_usage.cost_usd`
exactly once. It does not trust the then-broken parent failed-child rollup.

| episode | caps before (researcher/writer) | remembered changes applied | observable result | trace spend |
|---|---:|---|---|---:|
| 1 | 128 / 128 | none | both roles truncated; no brief | $0.025698 |
| 2 | 128 / 128 | none | both roles truncated; typed actions mined, but one sibling action overwrote the other | $0.023690 |
| 3 | 128 / 128 | writer 128→256 | both roles still truncated; no brief | $0.027529 |
| 4 | 128 / 256 | writer 256→512 | repeated research failure and writer failure; no brief | $0.044856 |
| 5 | 128 / 512 | researcher 128→256; writer 512→1024 | both roles recovered on retry; brief created | $0.088973 |

**Clean-series trace spend: $0.211746.** One preliminary instrumentation run is
excluded because the defect under investigation dropped the billed retry usage, so
its own trace could not provide an honest spend total.

## Defects the curriculum exposed and fixed

1. The executable action path was shadow-only; safe actions can now be explicitly enabled.
2. Delegated roles use `agent_runner_tool`, which neither mined nor applied ActionMemory.
3. Failed max-token retries lost their usage signal and billed token counts.
4. Company/agency scope was ambiguously inferred from underscore-delimited collection names.
5. Sibling actions collided on identical text, and promoted actions disappeared because detection scanned only `success_pattern` entries.
6. Failed delegated calls with valid usage were omitted from parent cost rollups.

Execution remains fail-closed. The only allowlisted mutation is `configure_role.max_tokens`;
the role must belong to the manager, the current cap must match the stored precondition,
and the new cap must be the recorded retry cap, no more than 2× and no more than 32,768.
Source rosters are never edited: only compiled run configs change.

## Next real-life failure curricula

### Support HQ — missing customer follow-up

Seed an incident with confirmed impact and rollback but no follow-up timestamp. The first
response commonly omits a further-update commitment. Score externally, store the specific
rubric failure, then test on a different incident. Success means the new response includes
a safe follow-up without leaking internal configuration.

### Reliability Labs — unsupported production-resolution claim

Mix a stale “resolved” note from an earlier environment with current rollback evidence and
an unresolved production check. The lesson must require current deployment evidence before
saying a fix is live. Test on a different release gate with the same stale-evidence trap.

### Revenue Ops — hypothesis presented as buying intent

Add a CRM hypothesis and an analyst recommendation beside sparse account facts. The learned
rule must preserve evidence labels and prevent invented buying intent, customer results, or
metrics on a different account holdout.

### New company candidate — Billing Guard

Simulate a payment webhook timing out after the charge succeeds. The first retry duplicates
the operation. A sandbox verifier can teach the company to use an idempotency key, then test
the lesson against a different provider/error sequence. This provides a deterministic,
tool-verifiable failure instead of another prose-only rubric.

## Requirements for a publishable benchmark

- Freeze Fabri and roster revisions before all measured arms.
- Use novel holdouts from the same failure family, never the identical task.
- Run paired memory and true no-memory controls with mining, retrieval, and action execution disabled in controls.
- Pre-register deterministic artifact/rubric checks and count incomplete runs as incomplete.
- Use enough replicas for paired inference; this five-episode engineering smoke is not evidence of a general memory benefit.
