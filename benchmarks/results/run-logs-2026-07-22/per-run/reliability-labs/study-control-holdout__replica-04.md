| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-04 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.018275 |
| total_retries | 0 |
| raw verdict | True |
| corrected verdict | True |
| negation-FP list | [] |

## Final output

## Release-readiness recommendation: **No-go**

The checkout change is not ready for release.

- **Blocking code defect:** Discount calculation treats percentages as fractions (`10` produces an invalid negative total rather than a 10% discount). Apply the percent conversion fix and add focused regression coverage.
- **QA evidence is insufficient:** QA tests could not run because `pytest` is missing from the test environment. Restore the dependency and rerun checkout and production-configuration tests.
- **Rollback evidence:** There is evidence that the prior release (2026.04.12) caused checkout HTTP 500s and was rolled back at 09:10 UTC; error rates returned to baseline by 09:18 UTC. This supports rollback effectiveness for that incident, but does not validate this change’s rollback procedure.
- **Required production-configuration verification:** Before any rollout, verify the required payment-provider environment variable is present in production, discount inputs use percentage units, promotion/feature-flag settings are correct, and payment safeguards reject invalid or negative charge amounts.
- **Deployment status:** No crew supplied evidence that a corrective fix has been deployed. Do not represent the fix as deployed.

**Release gates:** implement and test the calculation fix; restore and pass automated tests; verify the production configuration above; confirm the deployable artifact/version contains the fix; and validate rollback for the candidate release.

**Total run cost:** **$0.031881**