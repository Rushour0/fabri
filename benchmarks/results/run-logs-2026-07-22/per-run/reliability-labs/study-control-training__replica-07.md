| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-training |
| replica / variant | replica-07 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.018933 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational brief

### Observed evidence
- The confirmed code issue is in `store.cart_total`: it applied `discount_percent` as a fractional value rather than converting percentage units.
- For the documented contract case, `cart_total(100, 10)`, the original formula produced `-900` instead of the expected `90`.
- Available evidence indicates a focused production module (`store.py`) and focused test (`test_store.py`).
- QA evidence is inconsistent about test layout/module naming (`cart.py` versus `store.py`) and must not be treated as confirmation of additional defects.

### Rollback / remediation status
- A narrow remediation was applied: divide `discount_percent` by 100 before calculating the discounted total.
- No evidence indicates schema, persistence, dependency, or API-shape changes.
- Rolling back this remediation would restore incorrect totals for every non-zero percentage discount.
- Validation of either the rollback state or the remediated state is **not complete**: `pytest` could not run (exit 127), and `pip` was also unavailable in the QA environment.

### Unresolved verification work
1. Restore a usable Python test environment (`pip` and `pytest`, or equivalent) and execute the focused test suite.
2. Reconcile the conflicting QA references to `cart.py`/`cart_total` and the triage evidence for `store.py`/`test_store.py`; establish the authoritative source and test location.
3. Add and run boundary/contract tests for 0%, 100%, fractional-form inputs, negative and >100% discounts, rounding/monetary precision, and accepted input ranges.
4. Verify caller compatibility: current evidence supports percentage inputs such as `10` for 10%, but callers supplying fractional inputs such as `0.10` have not been assessed.

**Total run cost: $0.058310.**