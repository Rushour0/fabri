| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-training |
| replica / variant | replica-03 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.018947 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

### Observed evidence
- Production release **2026.04.12** deployed at **09:02 UTC**.
- Checkout HTTP 500 alerts fired at **09:06 UTC**.
- The on-call team initiated rollback at **09:10 UTC**; errors declined and returned to baseline by **09:18 UTC**.
- At **09:35 UTC**, the incident crew identified that the release expected a payment-provider environment variable absent from production.
- Separately, bug triage found a confirmed discount-calculation defect: `subtotal * (1 - discount_percent)` treats a percentage as a fraction. For `cart_total(100, 10)`, it yields `-900` while the supplied test expects `90`.

### Rollback
- The production release was rolled back at **09:10 UTC**.
- Observed result: checkout error rates returned to baseline by **09:18 UTC**.
- No rollback/version metadata was available to triage, and no rollback was performed for the discount-calculation defect.

### Unresolved verification work
- Confirm the required payment-provider environment variable is now correctly configured in production before any redeployment.
- Establish deployment preflight validation for required environment variables.
- Apply and review the discount calculation correction (`discount_percent / 100`), then run focused and broader behavioral tests.
- QA could not execute tests: `pytest` and `pip` were unavailable, and `unittest` discovered no tests. Provision a test-capable environment and implement/run coverage for normal, boundary, and negative-input cases.
- Reconcile the incident’s checkout-release evidence with the separately reported cart/discount code evidence before asserting a single causal chain.

**Total run cost: $0.03437.**