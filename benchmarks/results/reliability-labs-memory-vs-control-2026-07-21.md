# Company memory vs control study

> **Finding:** memory retrieves trained guidelines on every run (2 vs 0 for control) but does **not**
> improve reliability — the memory arm scored 60% vs the control's 78% rubric (−17.8pp), while costing
> ~19% more. This matches the Support HQ result (memory 7/10 vs control 9/10). Two companies now show
> the same pattern: trace-backed memory fires reliably but does not pay on these workloads.

- Case: `reliability_labs_incident_release_gate`
- Company: `reliability-labs`
- Fabri version: `0.18.5`
- Roster revision: `081536872d9dcfae30f033971b84a2f023065d05`
- Company source SHA-256: `de8ba1674d567168e349f142cff23009557c87e61b11569338ce9c49529276c3`
- Claim boundary: This measures this roster company's related-task performance when its learned SQLite memory is copied into a fresh holdout compile; it does not establish general memory effectiveness.

| Condition | Completion | Holdout rubric | Median cost | Mean cost | Mean guidelines |
|---|---:|---:|---:|---:|---:|
| memory | 100% | 60% | $0.1097 | $0.1150 | 2.00 |
| control | 90% | 78% | $0.1008 | $0.0966 | 0.00 |

| Memory − control | Rubric pass-rate delta | Mean cost delta | Mean guidelines delta |
|---|---:|---:|---:|
| Difference | -17.8% | $+0.0184 | +2.00 |

Incomplete holdouts have no rubric verdict. Prompts, traces, session IDs, and raw model output remain under `private-attempts/`.
