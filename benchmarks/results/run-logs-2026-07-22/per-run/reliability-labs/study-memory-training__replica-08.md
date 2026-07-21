| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-training |
| replica / variant | replica-08 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.017875 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational brief

### Observed evidence
- Bug triage found `store.cart_total(subtotal, discount_percent=0)` implemented as:
  ```python
  return subtotal * (1 - discount_percent)
  ```
- Existing test evidence expects `cart_total(100, 10) == 90`; the current implementation yields `-900`. This is consistent with treating whole percentage points as fractions rather than converting `10` to `0.10`.
- QA evidence is internally inconsistent with triage: QA references `src/cart.py` and reports no executable tests, while triage observed `store.py` and `test_store.py`. Treat the triage file/test observations as unverified until the repository state is reconciled.
- QA attempted `pytest`; it was unavailable (exit code 127). No automated test result is available.

### Rollback
- No deployment, incident timeline, version-control rollback record, or rollback action was found.
- No files were modified and no rollback was performed.

### Unresolved verification work
1. Reconcile the actual repository layout and canonical test suite (`store.py`/`test_store.py` versus `src/cart.py`/`tests`).
2. Confirm the input contract: whether `10` means 10% and how fractional, negative, above-100, and non-numeric discounts must behave.
3. Establish monetary precision and rounding requirements.
4. Provision the test runner or otherwise make the test environment executable, then run focused regression tests.
5. Verify whether external callers depend on the current behavior before implementing a correction.

**Status:** Partial — a calculation defect is evidenced, but rollback status and end-to-end verification cannot be confirmed.

**Total run cost:** $0.020767