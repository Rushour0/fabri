| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-training |
| replica / variant | replica-08 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.01943 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

### Observed evidence
- Production release **2026.04.12** deployed at **09:02 UTC**. Checkout HTTP 500 alerts fired at **09:06 UTC**.
- The incident crew reports the release required a new payment-provider environment variable that was absent in production; this was identified at **09:35 UTC**.
- Separately, triage found `cart_total()` applies percentage discounts as fractions: a 10% discount on 100 evaluates to **-900** rather than the expected **90**. This is a confirmed unit mismatch in the function examined.
- QA did not execute tests: the test directory was empty and `pytest` was unavailable. Therefore, no automated behavior or rollback validation is established.

### Rollback
- The production release was rolled back at **09:10 UTC**.
- Checkout error rates declined immediately and returned to baseline by **09:18 UTC**.
- No evidence was provided of a rollback for the `cart_total()` defect; its deployment status and production impact remain unknown.

### Unresolved verification work
1. Add pre-deployment validation that required production environment variables exist and are correctly configured.
2. Confirm the payment-provider configuration in the corrected release before redeployment.
3. Correct `cart_total()` by converting percentage input to a fraction, then verify 0%, fractional, 100%, negative, and over-100% cases against agreed business rules.
4. Install/configure `pytest`, create executable tests, and run them to validate both normal and edge-case cart behavior.
5. Establish whether the pricing defect reached production and, if so, assess affected transactions.

**Total run cost: $0.033175 USD.**