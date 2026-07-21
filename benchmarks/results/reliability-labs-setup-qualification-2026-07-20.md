# Company setup qualification

- Case: `reliability_labs_incident_release_gate`
- Company: `reliability-labs`
- Status: **no_viable_setup**
- Recommendation: `none`
- Fabri version: `0.18.5`
- Roster revision: `533f4f23081625ec1a92c4e562b6489f167561b7`
- Roster worktree clean: `True`
- Company source SHA-256: `de8ba1674d567168e349f142cff23009557c87e61b11569338ce9c49529276c3`
- Company source path: `companies/reliability-labs/company.toml`
- Released gate cost: —
- Total research spend: $0.189186
- Claim boundary: setup qualification only; memory/control result pending

| Candidate | Model runs | Decision | Completion | Conditional rubric | End-to-end | Median cost | Qualifies |
|---|---:|---|---:|---:|---:|---:|---:|
| baseline | 3 | — | 100% (3/3) | 2/3 (67%, 95% CI 21-94%) | 2/3 (67%, 95% CI 21-94%) | $0.0578 | no |
| delegated_artifact_tokens_256 | 0 | candidate_noop | 0% | — | 0% | — | no |

Incomplete runs are operational failures and have no rubric verdict. Raw prompts, traces, session IDs, and model output remain under `private-attempts/`.
