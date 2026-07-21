| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-08 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.01795 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **Do not release**

All three crews identified insufficient release evidence; Bug Triage found a **critical checkout-pricing defect**: percentage discounts appear to be treated as fractional rates (e.g., a 10% discount on 100 can yield -900 instead of 90). This is a release blocker until corrected and validated.

**Required verification before approval**
- Correct the discount-percentage calculation and run the stated regression test plus expanded boundary, validation, rounding, tax/discount, and payment-path tests.
- **Verify missing production configuration:** input contract (whole percent vs. fractional rate), server-side discount validation, currency rounding policy, payment/order-system compatibility, tax interaction, feature flag/progressive rollout, and monitoring/alerts.
- **Rollback evidence:** the postmortem crew reports a prior rollback initiated at 09:10 UTC with normal checkout restored by 09:18 UTC after an environment-variable issue; however, Bug Triage and QA found no supporting deployment, CI/CD, manifest, or rollback-plan artifacts. Treat rollback capability as **not independently verified** for this release.
- **Fix deployment:** no crew supplied evidence that a corrective fix was deployed. No deployment claim should be made.

**Total run cost:** **$0.033984**