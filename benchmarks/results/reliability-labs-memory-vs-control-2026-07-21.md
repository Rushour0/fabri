# Company memory vs control study

> **Finding:** memory retrieves trained guidelines on every run (2 vs 0 for control) but does **not**
> improve reliability — the memory arm scored 6/10 (60%, 95% CI 31-83%) vs the control's completed-only
> 7/9 (78%, 95% CI 45-94%), while costing ~19% more. **Denominator correction:** control completed only
> 9 of the scheduled 10 replicas, so 7/9 (78%) is not on the same basis as memory's 7/10. Presented
> honestly against the same 10-replica basis, control is 7/10 (70%, 95% CI 40-89%), making the
> conservative delta memory 6/10 (60%) vs control 7/10 (70%), a **−10pp** gap — not the −17.8pp implied
> by comparing 60% against the completed-only 78%. This matches the Support HQ result (memory 7/10 vs
> control 9/10). Two companies now show the same pattern: trace-backed memory fires reliably but does
> not pay on these workloads.

- Case: `reliability_labs_incident_release_gate`
- Company: `reliability-labs`
- Fabri version: `0.18.5`
- Roster revision: `081536872d9dcfae30f033971b84a2f023065d05`
- Company source SHA-256: `de8ba1674d567168e349f142cff23009557c87e61b11569338ce9c49529276c3`
- Claim boundary: This measures this roster company's related-task performance when its learned SQLite memory is copied into a fresh holdout compile; it does not establish general memory effectiveness.

| Condition | Completion | Holdout rubric (completed-only) | Holdout rubric (of 10 scheduled) | Median cost | Mean cost | Mean guidelines |
|---|---:|---:|---:|---:|---:|---:|
| memory | 100% (10/10) | 6/10 (60%, 95% CI 31-83%) | 6/10 (60%, 95% CI 31-83%) | $0.1097 | $0.1150 | 2.00 |
| control | 90% (9/10) | 7/9 (78%, 95% CI 45-94%) | 7/10 (70%, 95% CI 40-89%) | $0.1008 | $0.0966 | 0.00 |

Control completed only 9 of 10 scheduled replicas; the "completed-only" column (7/9) is the naive
in-sample rate, but it is not comparable to memory's 10-replica basis. The "of 10 scheduled" column
treats the missing replica conservatively and is the basis for the delta below.

| Memory − control | Rubric pass-rate delta (same 10-replica basis) | Mean cost delta | Mean guidelines delta |
|---|---:|---:|---:|
| Difference | **−10pp** (60% vs 70%) | $+0.0184 | +2.00 |

Note: an earlier version of this report compared memory's 60% against control's completed-only 78%
and reported a −17.8pp (rounded −18pp) delta. That understated control's true denominator (9, not 10,
replicas completed) and overstated the gap. The corrected, same-basis comparison is −10pp.

Incomplete holdouts have no rubric verdict. Prompts, traces, session IDs, and raw model output remain under `private-attempts/`.
