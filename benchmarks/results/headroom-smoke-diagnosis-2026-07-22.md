# Headroom smoke runs — three-layer diagnosis (2026-07-22)

Three 2-replica smoke runs of `support_hq_safe_incident_response` against the rewritten
(headroom) dataset. Total spend ≈ $0.81. No full run was spent; the pre-registered
go/no-go rule stopped each round at the diagnosis.

| run | change under test | control | memory | verdict |
|---|---|---|---|---|
| `smoke-headroom-20260722` | rewritten holdout | fail 2/2 | invalid 2/2 (`training_memory_db_missing`) | headroom real; transport check over-strict |
| `smoke-headroom-20260722-r2` | transport fix (`cfb2f15`) | fail 2/2 | fail 2/2, 7–8 guidelines retrieved | retrieval works; lesson truncated at 30 tokens |
| `smoke-headroom-20260722-r3-cap120` | `--guideline-max-tokens 120` (`9fab860`) | fail 2/2 | fail 2/2, lesson intact | lesson carries the **wrong branch** |

## What the instrument proved

**Headroom is real.** The control failed all 6 completed arms across all three runs: every
structured decision field wrong (`wrong:evidence_state`, `wrong:response_mode`,
`wrong:detail_policy`, `wrong:followup`), plus intermittent forbidden-identifier leaks
(`2026.04.12`). A no-memory company can no longer read the answers off the prompt. The
holdout rewrite achieved its design goal.

**Memory loses for engine reasons, not instrument reasons.** Each layer was verified by
direct artifact inspection, not inference:

1. **Transport (fixed).** The study aborted a memory arm when *any* declared memory DB was
   absent after training. The rewritten training prompt never routes to the
   incident-postmortem crew, so its DB legitimately never exists. Fixed in `cfb2f15`:
   fail closed only when zero DBs exist; absences recorded as `training_dbs_absent`.

2. **Capacity (knob added, default unchanged).** Mined lessons are hard-capped at 30 tokens
   (`memory.guideline_max_tokens`, `fabri/memory/compress.py`). The stored lesson in r2 was
   143 chars ending `"Action: Map MITIGATED. Expected outcome:..."` — three of the four
   protocol tokens destroyed at mining time. Retrieval injects only `entry.text`
   (`orchestrator/retrieval.py:848`), so nothing else can carry the payload. `9fab860` adds
   `--guideline-max-tokens` (recorded in `retrieval_overrides` provenance). At cap 120 the
   lesson survives storage intact.

3. **Supply selection (open — the real finding).** With capacity fixed, mining stored the
   **applied branch, not the declared protocol**. Training establishes the SHQ-E1 protocol
   (both branches) and applies branch A; the r3 lessons read
   `"Action: Use REPORT_ONLY, DIRECT_REPLY, CUSTOMER_FACTS, VERIFY_ACCOUNT"` — branch A
   verbatim. The holdout requires branch B (`MITIGATED / EXTERNAL_STATUS / CLASS_ONLY /
   MONITOR_UPDATE`), which appears nowhere in any stored entry. The miner compresses *what
   happened*; it does not extract *what was decreed for later work*.

   The engine has a channel designed for exactly this — the `<!-- AGENT_MEMORY -->` block
   (instructions compiled into every root manager; parsed by
   `fabri/memory/output.py::split_agent_output`; folded into mining via
   `orchestrator/pipeline.py::extract_agent_memory`). The training prompt explicitly ordered
   the mapping preserved in that block. **The model emitted it in 0 of 4 memory-arm training
   runs.** The self-report channel exists end to end and is simply not exercised by the
   model under test.

## Claim boundary

These runs demonstrate the *instrument* and localize the *engine limitation*. They are
2-replica smokes: directional, not statistical. No memory-vs-control result should be
published from them. The honest headline is: *the repaired benchmark detected, on first
contact, that fabri's passive mining cannot carry a multi-branch convention — the exact gap
the memory-as-action redesign targets.*

## Reproducing

```bash
set -a; source .env; set +a
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY
export FABRI_ROSTERS_ROOT=/path/to/fabri-rosters
uv run python -m fabri.benchmarks.company_memory_study \
  --dataset benchmarks/datasets/company_memory_experiments.yaml \
  --case support_hq_safe_incident_response \
  --output-dir benchmarks/runs/<name> --replicas 2 --guideline-max-tokens 120
```

Artifacts inspected: `private-attempts/replica-*/memory/training-compiled/support-hq/.fabri/*.db`
(stored lesson text), `private-attempts/replica-*/memory/private/result.json`
(`structured_output`, funnel), `training-run.stdout` (AGENT_MEMORY emission count).
