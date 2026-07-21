| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-control-training |
| replica / variant | replica-02 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.019967 |
| total_retries | 1 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

### Observed evidence
- Release **2026.04.12** deployed to production at **09:02 UTC**.
- Error-rate alerts triggered at **09:06 UTC**; customers received HTTP 500 responses until recovery at **09:18 UTC**.
- The incident crew reported that a required payment-provider production environment variable was absent; this was identified at **09:35 UTC**.
- Separately, bug triage found a confirmed cart-discount calculation defect: whole-number percentages (for example, `10`) are treated as fractional rates, making `cart_total(100, 10)` evaluate to `-900` rather than the expected `90`.
- QA did not execute tests, could not access `cart.py`, and found no existing test files in the inspected `tests` directory. Its proposed test plan is therefore not execution evidence.

### Rollback
- On-call rolled back Release 2026.04.12 at **09:10 UTC**.
- Error rates returned to baseline by **09:18 UTC**, which is evidence of operational recovery following rollback.
- No code change was made for the cart-discount defect; consequently, no code rollback was needed.
- QA has **not independently verified rollback behavior**.

### Unresolved verification work
1. Verify all required production environment variables, especially payment-provider configuration, and add deployment-time validation.
2. Confirm the missing environment variable is the causal release issue through reproducible configuration/deployment validation.
3. Apply and test the proposed discount fix (`discount_percent / 100`), including 0%, fractional, 100%, negative, and over-100% inputs.
4. Establish caller compatibility for normalized discount values such as `0.10`.
5. Implement and run automated tests for cart behavior and execute rollback/recovery validation in CI or a representative environment.

**Total run cost: $0.030576.**