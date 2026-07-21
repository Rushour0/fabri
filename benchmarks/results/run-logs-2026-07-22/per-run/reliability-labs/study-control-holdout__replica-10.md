| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-10 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.019668 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **BLOCK / not ready for production**

All three crews were consulted. The strongest directly observed evidence is that checkout discount calculation was incorrect (`10%` of `100` produced `-900`); a local source correction exists, but **there is no evidence that the fix was deployed**.

**Release blockers**
- **Testing is not verified:** the discovered checkout test could not run because Python/pytest were unavailable. QA also reports missing/unexecuted test assets and no reliable automation evidence.
- **Rollback evidence is insufficient/conflicting:** one incident assessment reports a prior rollback at 09:10 UTC after a 09:02 UTC release and recovery in error rates, but triage and QA found no rollback runbook or repository-level rollback evidence. Treat the reported incident rollback as historical evidence to validate, not as a release-approved rollback plan.
- **Production configuration verification is required:** verify the required payment-provider environment variable is present in production before release. The incident assessment attributes HTTP 500s to this missing configuration.
- **Operational safeguards remain unverified:** positively verify the deployed artifact/version, currency rounding and invalid-discount handling, monitoring for abnormal/negative totals, and a known-good rollback target.

**Required exit criteria**
1. Run checkout tests successfully in CI, including discount boundaries, invalid inputs, and currency/rounding behavior.
2. Verify the payment-provider production environment variable and other checkout configuration before promotion.
3. Confirm the corrected build is the artifact actually deployed.
4. Document and exercise/validate rollback to a known-good release.
5. Confirm monitoring and alerts for checkout failures and abnormal totals.

**Deployment statement:** No crew supplied evidence of a deployed fix; therefore, no fix deployment is claimed.

**Total run cost:** **$0.063326 USD**