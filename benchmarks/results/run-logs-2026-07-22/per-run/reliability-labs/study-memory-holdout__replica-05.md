| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-holdout |
| replica / variant | replica-05 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.02059 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **Do not release**

All crews identify material blockers:

- **Confirmed functional defect:** `cart_total(100, 10)` returns `-900` rather than `90`; percentage discounts are treated as fractional rates without `/100`.
- **No remediation evidence:** Triage found the faulty implementation still present and no corrected artifact or post-fix tests.
- **QA evidence is insufficient:** No checkout automated tests were found or executed, so there is no regression, edge-case, or post-fix validation.
- **Rollback evidence:** Incident evidence records a rollback at **09:10 UTC** after checkout HTTP 500s following the April 12 production deployment; error rates returned to baseline by **09:18 UTC**. This supports that rollback restored the prior service state, **not** that a corrected checkout fix was deployed.
- **Required verification item — production configuration:** Before any release, verify the required new payment-provider environment variable is configured in production, and verify the active production artifact/version and routing contain the intended percentage-conversion correction. The missing production configuration was implicated in the prior incident.
- **Do not claim a fix is deployed:** No crew supplied evidence of a corrected build being deployed or selected by production configuration/routing.

**Release gate:** implement and test the discount correction; validate required production configuration; prove deployed artifact/version and routing; and rehearse/verify rollback criteria before approval.

**Total run cost:** **$0.030411**