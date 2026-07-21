| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-holdout |
| replica / variant | replica-09 / null |
| outcome | success |
| complete | False |
| cost_usd | 0.01883 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **Do not release**

All three crews identify release blockers:

- **Confirmed functional defect:** the checkout discount calculation interprets a whole-percent value such as `10` incorrectly, producing invalid totals (e.g., $100 at 10% becomes -$900 instead of $90). The supplied behavioral test fails.
- **Test evidence is insufficient:** QA found no implemented test suite (`/tests` contains only `.gitkeep`) and `pytest` is unavailable in the environment. There is no post-fix validation evidence.
- **Production configuration — required verification item:** the payment-provider environment variable was missing in production during the prior release incident. Before approval, explicitly verify required payment credentials/mode, webhook/signature secrets, feature flags, tax/currency/rounding settings, secret provisioning, and checkout monitoring in the production target.
- **Rollback evidence:** There is evidence that Release 2026.04.12 was rolled back at 09:10 UTC after checkout 500s began at 09:06 UTC, with normal operations restored by 09:18 UTC. However, no evidence was supplied for a current release rollback artifact, owner/runbook, tested flag-disable path, migration reversibility, or preservation of in-flight payment state.
- **Deployment status:** No crew supplied evidence that a corrected fix was deployed; therefore, no fix is being claimed as deployed.

**Approval criteria:** correct and test the percentage calculation; implement and execute checkout tests; record successful production-configuration verification; and demonstrate/document a rollback procedure for the release artifact.

**Total run cost:** **$0.033388**