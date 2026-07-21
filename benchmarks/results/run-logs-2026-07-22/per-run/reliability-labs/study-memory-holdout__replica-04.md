| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-holdout |
| replica / variant | replica-04 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.019287 |
| total_retries | 0 |
| raw verdict | False |
| corrected verdict | True |
| negation-FP list | fix was deployed |

## Final output

## Release-readiness recommendation: **Do not release**

All three crews identify release-blocking gaps:

- **Functional defect:** Triage reports `cart_total()` interprets percentage discounts incorrectly; a 10% discount on 100 can produce `-900` rather than `90`. Correct the percentage conversion and validate boundary cases and currency rounding.
- **Rollback evidence:** Production release **2026.04.12** was deployed at 09:02 UTC; checkout HTTP 500 alerts began at 09:06; on-call rolled it back at 09:10; error rates returned to baseline by 09:18. This supports rollback effectiveness, but **no rollback test/automation validation was executed**.
- **Missing production configuration — required verification item:** The payment-provider environment variable required by the release was absent in production. Before any redeploy, verify the variable and all dependent production payment configuration, manifests/flags, and preflight validation.
- **Test evidence:** QA could not run `npm test -- --coverage` because `package.json` was missing (exit 254 / `ENOENT`). Triage also reports the existing discount test is expected to fail. Establish the correct test environment and obtain passing execution evidence.
- **Deployment status:** **No crew supplied evidence that a corrective fix was deployed.** The only documented production action is the rollback.

**Release gate:** approve only after the discount fix is implemented, automated tests pass in the correct environment, production payment-provider configuration is verified through preflight, and a controlled rollout demonstrates stable checkout behavior with rollback readiness.

**Total run cost:** **$0.028768**