| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-05 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.017743 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **NO-GO / hold release**

All crews identify material readiness gaps. The strongest defect evidence is a checkout discount calculation error: a 10% discount on 100 yields `-900` rather than `90`, indicating percentage points are handled as fractions. Test execution was not possible in the available workspace, and QA found no implemented test suite or validation evidence.

**Required verification before promotion**
1. Correct the discount percentage conversion and run focused and boundary tests (0%, 10%, 100%, invalid values) in a runnable CI/test environment.
2. **Production configuration:** verify and document the required payment-provider environment variable/configuration is present in production before deployment. A prior release incident attributes checkout HTTP 500s to this missing variable.
3. **Rollback evidence:** the previous release was rolled back at 09:10 UTC after checkout error alerts, with error rates returning to baseline by 09:18 UTC. For this release, verify the rollback procedure, artifact/version selection, and post-rollback health checks before approving promotion.
4. Provide deployment/promotion evidence and release metadata.

**Fix deployment status:** No crew supplied evidence that a corrective fix was deployed to any environment; therefore, no fix deployment is claimed.

**Total run cost:** **$0.045834**