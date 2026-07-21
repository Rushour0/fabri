| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-training |
| replica / variant | replica-03 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.01875 |
| total_retries | 1 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

**Observed evidence**
- Bug triage found `store.py` documents `cart_total(subtotal, discount_percent)` as applying a percentage discount, while the implementation uses `subtotal * (1 - discount_percent)`.
- The visible assertion `cart_total(100, 10) == 90` conflicts with that implementation: static evaluation yields `-900`.
- This supports a **high-confidence percentage-unit mismatch**: callers/tests appear to supply whole percentages (`10`), while code treats the input as a fractional rate (`0.10`).
- No test suite was executed. QA reporting also confirms no executed rollback validation or coverage data.
- The postmortem crew could not identify incident-specific timeline, impact, or deployment evidence because none was supplied.

**Rollback**
- There is **no observed evidence** of a deployed release, prior known-good revision, release artifact, or completed rollback.
- Accordingly, no rollback can be factually confirmed or recommended from the available material.
- Before rolling back or patching, verify historical behavior and whether external callers use whole-percent versus fractional discount inputs.

**Unresolved verification work**
1. Run the visible tests and preserve actual output.
2. Inspect all callers and API documentation to establish the intended discount unit and accepted range.
3. Check version/deployment history for whether the defect was introduced recently and whether a safe rollback target exists.
4. Add and execute tests for 0%, 10%, 100%, invalid/out-of-range inputs, rounding, and the chosen unit convention.
5. Confirm monetary precision requirements; floating-point arithmetic may be unsuitable depending on the contract.
6. Obtain incident timeline, user impact, CI logs, and deployment metadata before completing a postmortem.

**Total run cost:** **$0.052537**