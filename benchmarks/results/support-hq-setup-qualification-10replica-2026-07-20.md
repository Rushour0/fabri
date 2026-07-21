# Company setup qualification

- Case: `support_hq_safe_incident_response`
- Company: `support-hq`
- Status: **no_viable_setup**
- Recommendation: `none`
- Fabri version: `0.18.5`
- Roster revision: `533f4f23081625ec1a92c4e562b6489f167561b7`
- Roster worktree clean: `True`
- Company source SHA-256: `1ef191962a4b2a27d780fa087bb1a4fdb67e378a3030307e31b5b8386f15ffe0`
- Company source path: `companies/support-hq/company.toml`
- Released gate cost: —
- Total research spend: $0.229046
- Claim boundary: setup qualification only; memory/control result pending

| Candidate | Model runs | Decision | Completion | Conditional rubric | End-to-end | Median cost | Qualifies |
|---|---:|---|---:|---:|---:|---:|---:|
| baseline | 10 | — | 100% (10/10) | 9/10 (90%, 95% CI 60-98%) | 9/10 (90%, 95% CI 60-98%) | $0.0211 | no |
| delegated_artifact_tokens_256 | 0 | candidate_noop | 0% | — | 0% | — | no |

Incomplete runs are operational failures and have no rubric verdict. Raw prompts, traces, session IDs, and model output remain under `private-attempts/`.
