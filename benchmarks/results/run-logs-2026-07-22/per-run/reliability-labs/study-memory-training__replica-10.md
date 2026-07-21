| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-training |
| replica / variant | replica-10 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.017653 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational brief

### Observed evidence
- The confirmed defect was in `store.py`’s discount calculation: it treated `discount_percent` as a normalized decimal rather than a percentage.
- Before remediation, `cart_total(100, 10)` evaluated to `-900`; the existing expectation is `90`.
- QA reported no automated tests in the designated `tests` directory, while triage identified a focused `test_store.py` expectation. This discrepancy remains to be reconciled.
- No incident timeline, customer impact, or prior rollback record was provided or located.

### Rollback / remediation
- No rollback or revert was identified.
- A targeted source remediation was applied:
  - From: `subtotal * (1 - discount_percent)`
  - To: `subtotal * (1 - discount_percent / 100)`
- Source-level evaluation now yields `cart_total(100, 10) == 90`.
- No other behavior, validation, or rounding logic was changed.

### Unresolved verification work
- The focused verification command, `pytest -q test_store.py`, could not execute because `pytest` was unavailable on `PATH` (`exit 127`).
- Install/provision pytest and rerun the focused test, then run the relevant broader suite.
- Reconcile the reported test-location/coverage discrepancy.
- Define and test contract behavior for fractional, zero, negative, and greater-than-100 discounts; invalid types; and currency rounding.
- Obtain the incident timeline and impact data before completing a postmortem.

**Total run cost: $0.060543 USD**