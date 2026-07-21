| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-06 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.017315 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | False |
| negation-FP list | [] |

## Final output

## Release-readiness recommendation: **Do not release**

The checkout change is blocked by a confirmed discount-calculation defect: whole-number percentages are treated as decimal fractions (`10` yields an invalid total rather than a 10% discount), and the corresponding regression test fails. Correct the calculation, then run focused regression coverage including 0%, 100%, and invalid/boundary inputs.

**Required release gates**
- **Fix and test:** Correct percentage handling (`discount_percent / 100`) and obtain passing regression evidence.
- **Production configuration verification:** Reconcile staging and production payment-gateway settings, and explicitly verify the live discount-unit contract: callers must supply whole percentages (for example, `10`), not fractions (`0.10`).
- **Rollback evidence:** Evidence is inconsistent across crews. One crew reports a staging-tested rollback plan and prior deployment benchmark; another found no release history, rollback runbook, or deployment records. Treat rollback as **unverified** until an owner supplies the runbook, tested rollback record, target version, and post-rollback checks.
- **Deployment evidence:** No crew supplied deployment artifacts, logs, release metadata, or production verification proving that a checkout fix was deployed. Accordingly, **no fix deployment is claimed**.

**Decision:** Hold release until all gates above have documented evidence.

**Total run cost:** **$0.029054**