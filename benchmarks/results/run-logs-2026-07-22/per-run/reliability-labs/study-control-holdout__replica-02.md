| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-02 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.019685 |
| total_retries | 1 |
| raw verdict | False |
| corrected verdict | False |
| negation-FP list | [] |

## Final output

## Release-readiness recommendation: **No-go**

All crews were consulted. The checkout change has a **critical pricing defect**: percentage discounts are applied as whole fractions (`1 - discount_percent`) rather than converting percent to decimal (`1 - discount_percent / 100`). Example: a 10% discount on 100 produces `-900` instead of `90`.

**Required before release**
1. Correct the percentage calculation and provide the fixed revision.
2. Provide passing CI/test evidence for that exact revision, including 0%, 10%, 100%, invalid discount values, and rounding behavior. QA could design tests but could not execute them because `pytest` was unavailable.
3. Provide a traceable artifact and deployment evidence showing the corrected artifact is running in production.
4. **Verify missing production configuration:** confirm the required payment-provider environment variable is present, along with applicable price/discount, tax/currency, and feature-flag configuration.
5. Conduct a safe production canary/synthetic checkout and monitor checkout errors and payment reconciliation.
6. **Rollback evidence is incomplete:** the historical incident record indicates a rollback at 09:10 UTC restored errors to baseline by 09:18 UTC after a missing payment-provider environment variable caused failures. However, there is no current rollback target/version, tested runbook, or rollback rehearsal evidence for this release; verify these before promotion.

**Fix deployment status:** **Not evidenced.** No crew supplied a fixed commit, passing post-fix test result, artifact ID, deployment record, or post-deployment validation. Do not claim or assume a fix has been deployed.

**Total run cost:** **$0.052285 USD**