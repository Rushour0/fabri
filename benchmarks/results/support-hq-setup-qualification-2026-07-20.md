# Support HQ company setup qualification

Date: 2026-07-20

## Release claim

Support HQ's current baseline setup qualified for the incident-response
experiment. Three fresh runs completed the required delegation tree, passed the
frozen deterministic rubric, and stayed within the configured company cost
limit.

This qualifies the company setup for a later memory/control experiment. It is
not evidence that memory or retrieval improves outcomes, and it does not show
that Support HQ needs a company-specific runtime change.

## Method

- Source: `FABRI_ROSTERS_ROOT/companies/support-hq/company.toml`
- Workload: `support_hq_safe_incident_response`
- Replicas: 3, each with a fresh company compile and isolated `FABRI_HOME`
- Required delegations: `support_macro_writer` and
  `incident_postmortem_crew`
- Required response concepts: `checkout`; `rollback` or `rolled back`;
  `follow-up` or `further update`
- Forbidden response concepts: the payment-provider environment variable and
  blame
- Selection: a candidate must pass every scheduled replica; among qualifying
  candidates, choose the lowest median recursively accounted cost

Hyphens, whitespace, and case are normalized before phrase matching. The
accepted alternatives were frozen before the released runs. A recovered child
run counts as complete when Fabri reports `success_with_recovery`; truncation,
timeouts, missing traces, failed required delegations, and unaccounted cost
remain operational failures.

## Released result

| Candidate | Completion | Rubric given completion | End-to-end | Median cost | Decision |
|---|---:|---:|---:|---:|---|
| Baseline | 3/3 | 3/3 | 3/3 | $0.020200 | Qualified and recommended |
| Delegated artifact floor: 256 tokens | 0 scheduled | Not measured | Not measured | $0 | Rejected as a no-op |

The three released baseline runs cost $0.019853, $0.020443, and $0.020200, or
$0.060496 total. Earlier pilot and classifier-validation runs cost $0.212341.
Total live-model research spend for this qualification was $0.272837.

The no-op candidate was not sent to a model: every relevant delegated artifact
role in the compiled company already met or exceeded its proposed 256-token
floor.

## What we learned

1. The original 60-token signal was real but misattributed. It belonged to a
   narrator call recorded inside delegated execution, not the role responsible
   for the final artifact. Increasing artifact-role limits would therefore not
   have addressed that observation.
2. Company setup search should begin with the compiled delegation graph. A
   candidate that makes no effective change should be rejected before it spends
   credits.
3. Operational completion and output quality are separate measurements. An
   incomplete run has no scoreable rubric verdict; a complete run can still
   fail the rubric.
4. Deterministic rubrics need concept-level alternatives fixed in advance.
   Treating `rolled back` as different from `rollback`, or `follow up` as
   different from `follow-up`, measures orthography rather than company
   capability.
5. Recovery is part of Fabri's success model. A harmless failed path probe that
   is recovered should remain visible in the trace, but should not erase a
   completed, scoreable artifact.
6. The evidence supports the baseline. It does not justify a Support HQ override
   or a global runtime rule.

## Next experiment

Run the isolated training/holdout memory and fresh-company control arms. Copy
only the learned SQLite database from training into a fresh memory holdout
compile; do not carry training workspace files. Report completion, conditional
rubric pass rate, end-to-end pass rate, cost, and retrieved-guideline evidence.
Do not make a memory-benefit claim until that holdout comparison passes.
