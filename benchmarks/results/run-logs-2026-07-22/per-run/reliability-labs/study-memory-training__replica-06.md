| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-training |
| replica / variant | replica-06 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.016733 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational brief

**Observed evidence**
- A test specifies that `cart_total(100, 10)` must return `90`.
- The current calculation is `subtotal * (1 - discount_percent)`, which evaluates to `-900` for that input.
- This confirms the function applies a whole-number percentage as a fractional rate instead of converting `10` to `0.10`.
- No incident timeline, monitoring data, test-execution results, or prior incident artifacts were supplied. QA therefore did not provide independently executed validation evidence.

**Rollback / remediation**
- No rollback is evidenced or reported.
- No code change was made during triage.
- The minimal proposed correction is to apply `discount_percent / 100` before calculating the discounted total. This remains a recommendation, not a verified deployment.

**Unresolved verification work**
- Implement and run the focused regression: `cart_total(100, 10) == 90`.
- Confirm behavior for zero/default discount, decimal percent inputs, 100% discount, and invalid or out-of-range values.
- Run the relevant test suite and capture results.
- Confirm whether a rollback occurred and, if so, validate its production state using an incident timeline and deployment/monitoring evidence.

**Status:** Partial. The core calculation defect is evidenced, but remediation and rollback validation are still pending.

**Total run cost:** **$0.028702 USD**