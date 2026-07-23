# Headroom smoke, round 4 — supply-selection gap confirmed post-tag-fix (2026-07-24)

One 2-replica smoke of `support_hq_safe_incident_response` (rewritten holdout, `--guideline-max-tokens 120`),
run after three engine changes landed on main: durable company memory paths (#79), the
`<AGENT_MEMORY>` tag with tolerant parsing (#80), and the reuse-metric fix (#79).
Pre-registered outcomes from the 2026-07-22 handoff applied unchanged. Spend ≈ $0.32.

| replica | condition | holdout complete | rubric | guidelines retrieved | cost |
|---|---|---|---|---|---|
| 1 | memory | yes | **fail** (all 4 fields wrong) | 6 | $0.0826 |
| 1 | control | yes | **fail** (all 4 fields wrong) | 0 | $0.0732 |
| 2 | control | yes | **fail** (all 4 + forbidden leak `2026.04.12`) | 0 | $0.0758 |
| 2 | memory | yes | **fail** (all 4 + forbidden leak `2026.04.12`) | 7 | $0.0848 |

Outcome: **both arms fail** — the third pre-registered branch. Not published as a memory-vs-control
result; 2-replica smokes are directional only.

## What this round adds over the 2026-07-22 diagnosis

1. **Instrument interaction found and fixed.** The durable-memory change (#79) anchors compiled
   `sqlite_path`s at the invocation cwd, which escapes the study's per-replica company root — the
   manifest check correctly failed closed (`training_memory_manifest_invalid`, $0 spent). Fixed by
   adding `fabri company compile --run-from` and pinning the study to the replica's compiled tree,
   restoring hard replica/arm isolation. First round after the fix ran end to end.
2. **The `<AGENT_MEMORY>` tag alone does not exercise the self-report channel.** Zero emissions in
   all training-run stdout, matching the 0/4 rate from 2026-07-22's comment-marker form. The channel
   works when the model chooses to use it (observed live in an AgentWorks Studio run on 2026-07-23),
   but the model under test (gpt-5.6-terra) does not use it under this training prompt. Format was
   not the binding constraint.
3. **Supply selection remains the binding constraint, unchanged.** Stored lessons again carry the
   applied branch verbatim ("SHQ-E1 explicitly maps this condition to REPORT_ONLY" — branch A);
   the holdout requires branch B, present in no stored entry. Retrieval delivered 6–7 entries per
   memory arm; they contained the wrong knowledge. The miner compresses what happened, not what was
   decreed.

## Claim boundary

Headroom is real (control 0/4 across all four completed control arms in rounds 1–4). Fabri's
passive mining cannot yet carry a multi-branch convention across sessions, and neither the tag
format fix nor a 120-token cap changes that. The designed answer is the convention-mining proposal
(`docs/design/convention-mining-decisions-proposed-2026-07-22.md`, first release scoped to
`effect_class="response_mapping"`), which remains unratified. Do not spend on a full 6-replica run
until a supply-side change (convention mining or equivalent) lands.

## Reproducing

```bash
set -a; source .env; set +a
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
uv run python -m fabri.benchmarks.company_memory_study \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/smoke-headroom-20260724 --replicas 2 --guideline-max-tokens 120
```

Artifacts inspected: per-replica `training-compiled/support-hq/.fabri/*.db` (stored lesson text),
`private/training-run.stdout` (AGENT_MEMORY emission count: 0), `results.json` funnels.
