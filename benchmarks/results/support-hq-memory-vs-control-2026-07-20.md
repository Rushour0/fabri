# Company memory vs control study

> **Finding:** memory retrieves trained guidelines on every run (2 vs 0 for control) but does **not**
> improve reliability — the memory arm scored 7/10 vs the control's 9/10 (−20pp) at essentially equal
> cost. A preliminary 3-replica pilot showed the opposite (memory 3/3 vs control 2/3, +33pp); the
> 10-replica confirmation below reversed it — the same small-sample fragility seen in the setup-
> qualification study. On this workload the self-improvement loop runs but does not pay.

- Case: `support_hq_safe_incident_response`
- Company: `support-hq`
- Fabri version: `0.18.5`
- Roster revision: `533f4f23081625ec1a92c4e562b6489f167561b7`
- Company source SHA-256: `1ef191962a4b2a27d780fa087bb1a4fdb67e378a3030307e31b5b8386f15ffe0`
- Claim boundary: This measures this roster company's related-task performance when its learned SQLite memory is copied into a fresh holdout compile; it does not establish general memory effectiveness.

| Condition | Completion | Holdout rubric | Median cost | Mean cost | Mean guidelines |
|---|---:|---:|---:|---:|---:|
| memory | 100% | 70% | $0.0603 | $0.0600 | 2.00 |
| control | 100% | 90% | $0.0632 | $0.0614 | 0.00 |

| Memory − control | Rubric pass-rate delta | Mean cost delta | Mean guidelines delta |
|---|---:|---:|---:|
| Difference | -20.0% | $-0.0014 | +2.00 |

Incomplete holdouts have no rubric verdict. Prompts, traces, session IDs, and raw model output remain under `private-attempts/`.
