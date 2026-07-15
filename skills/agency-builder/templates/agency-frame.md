# Agency frame

Complete this before generating files. One agency should own one repeatable
deliverable, not an entire department.

| Field | Required decision |
| --- | --- |
| Agency name | Kebab-case name and one-sentence purpose. |
| Target persona | Who requests and consumes the deliverable? |
| One deliverable | The single file, record, or bounded result the agency produces. |
| Inputs and boundary | Required source files/data; paths, systems, and actions that are out of scope. |
| Specialist roles | The smallest fixed set of roles; state each role's artifact or decision. |
| Proof-bar metric | An observable pass condition, such as required headings, all source items covered, or a test suite. Restate it as an instrumented number the run reports, not a vibe. |
| Approval gate | Deterministic verifier, named human approver, or both; state what happens on failure. |
| Metrics and COGS | **Required, not optional.** See the dedicated section below. |
| Memory scope | A stable collection name and whether past related runs should be retained. |

## Metrics and COGS (required)

Fabri already computes cost (`src/fabri/pricing.py`, the `usage` event, `fabri
report`); an agency frame must decide how it is bounded and reported, or the
agency has no unit economics.

| Field | Required decision |
| --- | --- |
| Provider and step budget | Provider/key env var, maximum parent steps and child steps. |
| Cost ceiling | A concrete `agent.max_cost_usd` per run (fabri kills a run at `Outcome.BUDGET_EXCEEDED` once crossed). State the number, e.g. `0.50`. "No ceiling" is a decision that must be made explicitly, not by omission. |
| Reported metrics | Which run metrics the delivery message must quote: per-run total COGS, `cost_by_model`, `outcome`, and tool-failure rate at minimum; for a fan-out agency, also the fleet total COGS and cost-per-item. |
| Cost surface caveat | Note that static `tools.agents[]` specialist cost does **not** roll into the parent's `total_cost_usd` — report per-session and sum, or use the fleet roll-up which sums per-session. |

Stop and ask for clarification when the deliverable, proof bar, approval gate, or
cost ceiling is blank. A role list without those fields is a collection of
prompts, not an agency contract — and an agency with no cost ceiling and no
reported COGS is a demo, not a product.
