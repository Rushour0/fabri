| Field | Value |
| --- | --- |
| company | reliability-labs |
| phase | study-memory-training |
| replica / variant | replica-09 / null |
| outcome | success |
| complete | True |
| cost_usd | 0.01982 |
| total_retries | 0 |
| raw verdict | null |
| corrected verdict | null |
| negation-FP list | [] |

## Final output

## Operational Brief

### Observed evidence
- Incident postmortem reports release `2026.04.12` deployed at **09:02 UTC**; checkout HTTP 500 alerts fired at **09:06 UTC**.
- Checkout errors returned to baseline by **09:18 UTC**.
- At **09:35 UTC**, the team identified an absent production payment-provider environment variable required by the release.
- Separately, bug triage found a confirmed percentage-conversion defect in `store.py`: `cart_total(100, 10)` computes `-900` rather than the asserted `90`.
- QA reported no executed automated or rollback-validation tests. The crew’s repository observations conflict with triage’s reported test file, so test inventory must be independently reconciled.

### Rollback
- The production release was rolled back at **09:10 UTC**; errors declined afterward and baseline resumed at **09:18 UTC**.
- The rollback restored service behavior, but no automated rollback validation was reported.
- For the isolated discount calculation defect, rollback of a prospective correction would reintroduce incorrect totals for nonzero percentage discounts.

### Unresolved verification work
1. Confirm production configuration now includes all release-required environment variables, including the payment-provider variable.
2. Add deploy-time validation for required environment configuration and document release prerequisites.
3. Independently verify the test inventory and execute regression coverage for discount handling:
   - no discount, 10%, fractional percentages, 0%, and 100%;
   - defined behavior for negative or over-100% discounts and negative subtotals.
4. Create or complete automated coverage for `cart_total`, including zero price/quantity and negative-input validation.
5. Validate rollback behavior through monitored, repeatable checks rather than relying only on post-rollback error-rate recovery.

**Total run cost: $0.030523.**