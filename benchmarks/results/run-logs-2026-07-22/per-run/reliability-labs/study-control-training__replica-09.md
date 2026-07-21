| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-training |
| replica / variant | replica-09 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.01737 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

### Observed evidence
- Bug triage found `store.py:3` calculates `subtotal * (1 - discount_percent)`.
- The available expectation is `cart_total(100, 10) == 90`; under the identified formula, that input produces `-900`.
- This confirms percentage inputs are being treated as decimal fractions. Every non-zero percentage discount is therefore calculated incorrectly.
- QA reported no dedicated automated test suite or documented rollback-validation procedure in the inspected project structure. This conflicts with triage’s reference to an available `test_store.py`; the discrepancy requires reconciliation before asserting complete test coverage.
- No incident timeline, deployment history, production impact, or caller inventory was supplied.

### Rollback / remediation status
- **Rollback status: unknown.** No evidence establishes that a rollback occurred, which version was restored, or whether rollback validation ran.
- **Remediation not yet verified:** the smallest identified code correction is to divide `discount_percent` by 100 before applying it. No evidence shows this change has been applied or tested.

### Unresolved verification work
1. Reconcile repository/test evidence and run the actual full test suite after the fix.
2. Verify calculations for 0%, fractional, 100%, and policy-defined invalid/out-of-range discounts.
3. Identify affected callers and determine whether incorrect calculations reached production.
4. Obtain deployment/rollback records and validate the restored or corrected version against production behavior.
5. Establish regression coverage and a documented rollback-validation checklist.

**Total run cost:** **$0.030325 USD**