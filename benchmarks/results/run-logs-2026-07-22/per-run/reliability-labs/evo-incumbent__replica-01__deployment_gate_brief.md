| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | evo-incumbent |
| replica / variant | replica-01 / deployment_gate_brief |
| outcome | success |
| complete | True |
| cost_usd | 0.016532 |
| total_retries | null |
| raw verdict | True |
| corrected verdict | True |
| negation-FP list | [] |

## Final output

**Deployment gate: BLOCK / do not deploy checkout change.**

- **Verified defect evidence:** Triage found the percentage-discount calculation is incorrect: `cart_total(100, 10)` should be `90`, while the current formula would yield `-900`.
- **Rollback facts:** No rollback action, deployment history, or rollback result is evidenced.
- **Outstanding production verification:** No recorded CI/test execution, deployment state, runtime validation, monitoring evidence, affected-order assessment, or post-deploy transaction checks.
- **Remediation:** Not evidenced as live; the inspected source reportedly still contains the defect.

**Required to reopen gate:** correct percentage normalization, add/run focused checkout tests (including boundary and invalid inputs), obtain CI evidence, then perform documented production verification and monitoring.

**Total run cost:** **$0.033346**