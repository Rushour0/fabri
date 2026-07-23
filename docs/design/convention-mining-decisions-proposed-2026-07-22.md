# Proposed decisions for convention mining

**Date:** 2026-07-22  
**Status:** RATIFIED 2026-07-24 by the operator, as recommended (all five answers accepted unchanged; decision 5's numeric core gates flagged as first-to-revisit once shadow telemetry exists)  
**Scope:** answers the five open questions in
`convention-mining-research-2026-07-22.md`; no run data is claimed here

These recommendations keep convention mining behind an explicit opt-in and preserve fabri's
current fail-closed boundaries: task-origin content cannot escape quarantine by succeeding or
recurring, quarantine remains ineligible for retrieval, and convention text can never populate
`resolution` or cross the ActionMemory capability allowlist.

## 1. Who can create durable policy?

**Question:** Is trusted company/agency configuration sufficient, or must every task-mined convention receive explicit human approval before leaving quarantine?

**Recommended answer:** Treat an operator-selected company or agency configuration as sufficient
authority for its own scope; require explicit, hash-specific human approval for every convention
mined from task text, tool output, or model output. Add
`memory.convention_mining_enabled: false`, `memory.convention_trusted_sources: []`, and
`memory.convention_approvals: []`; an operator must both enable mining and add `company_config`
and/or `agency_config` to the trusted-source list.
A task-mined candidate is always stored as `verification="unverified", tier="quarantine"`, even
after a successful or rubric-verified run. Ratification consists of adding its canonical
`(scope, key, version, branch_mapping_hash)` to `memory.convention_approvals`; loading that exact
approval changes it to `verification="human_verified", tier="retrieve"`. Approval never uses
recurrence, `hit_count`, a semantic-similarity match, or a session verdict, and any mapping change
produces a new hash that needs a new approval. Config-origin records use
`verification="config_verified"` only when their source path came from the compiler-loaded,
operator-selected company/agency configuration and their scope is no broader than that source.

This fits the current model with small, explicit extensions: `MemoryEntry.verification` already
round-trips as a string, while `verification_allowed()` needs to recognize the two new verified
values. The unconditional `tier != "quarantine"` retrieval filter remains the final backstop.

**Strongest argument against:** A checked-in or compiler-loaded company file is not automatically
safe: generators, dependencies, or an overly broad maintainer group can alter it without the
policy review that “durable authority” deserves. Requiring a human approval for every source,
including config, gives one uniform audit trail and avoids treating repository write access as
policy-authoring authority.

**Cheapest disconfirming observable:** Audit the provenance and review path for the next ten
changes to trusted company/agency config. If any convention-bearing change can be generated,
merged, or deployed without the named policy owner reviewing its canonical mapping and scope,
config location is not sufficient authority and config-origin conventions also need hash-specific
human approval.

For SHQ-E1, the training task may produce a quarantined two-branch candidate but may not teach the
holdout until a human approves its exact mapping (or the same mapping is moved into a trusted
Support HQ config source). After approval, the memory arm should retrieve the convention; an
unapproved arm should retrieve no SHQ-E1 convention.

## 2. Which effect classes are allowed?

**Question:** Should the first release allow only structured response mappings, or also non-executable workflow conventions such as escalation and follow-up choices?

**Recommended answer:** Allow only `effect_class="response_mapping"` in the first release. Add
`memory.convention_allowed_effect_classes: [response_mapping]`; validate that every branch sets
only fields in a declaration-local response schema and contains no tool call, role delegation,
config path, credential, file mutation, network action, approval bypass, or external side effect.
An otherwise well-formed workflow convention remains `tier="quarantine"` with reason
`effect_class_not_allowed`; it is not rewritten into a response mapping. Convention ingestion
must force `resolution=None` and prohibit `tier="action"` regardless of the text. Executable
behavior continues to require the separate typed ActionMemory path, whose current implementation
accepts only its narrow `configure_role` token-cap recovery under hard idempotence, attempt, scope,
and cap checks.

**Strongest argument against:** Non-executable workflow choices—such as “escalate to legal” or
“schedule a follow-up review”—are often the most valuable institutional conventions, and excluding
them sharply limits recall and product value. A typed workflow enum with no automatic tool call
could be as safe as a response field, so the distinction may reflect implementation convenience
more than actual risk.

**Cheapest disconfirming experiment:** Shadow-classify the first 25 source-anchored, authorized
quarantined candidates without retrieving them. If reviewers judge at least 10 to be safe,
non-executable workflow choices that cannot be represented as response fields, while finding no
action-bearing ambiguity in those 10, the response-only boundary is too narrow.

SHQ-E1 is inside the allowed class because its branches map evidence to four response fields. If
this answer is right, the benchmark's memory arm should emit exactly `MITIGATED`,
`EXTERNAL_STATUS`, `CLASS_ONLY`, and `MONITOR_UPDATE` for the holdout while still omitting the
forbidden implementation and release identifiers; no action proposal or config write should be
observed.

## 3. How should ambiguous natural-language conditions behave?

**Question:** Should fabri let the model choose from a complete table, require a machine-evaluable predicate grammar, or fail closed and request task-local clarification?

**Recommended answer:** Preserve natural-language conditions, but fail closed when exactly one
branch cannot be selected. Add `memory.convention_ambiguous_condition_policy: clarify` and
`memory.convention_branch_selection_max_retries: 1`. Retrieval renders the complete atomic table
and requires the model to return one `selected_branch_id` plus current-run evidence before copying
any mapped values. A deterministic validator accepts the mapping only if the branch ID exists, all
returned mapped fields exactly equal that branch, and no second branch was selected. On a missing,
multiple, or inconsistent selection, fabri makes at most one corrective selection attempt; if it
still lacks one valid branch, it applies no convention fields and asks a task-local clarification
(or returns an explicit `convention_not_applicable` result when interaction is unavailable).
Extraction-time unresolved referents, omitted conditions, or duplicate/overlapping normalized
conditions keep the whole record in quarantine rather than weakening this application rule.

**Strongest argument against:** This still delegates semantic predicate evaluation to the model,
so it cannot reliably detect two subtly overlapping natural-language branches; the deterministic
validator only checks the shape and fidelity of the model's choice. Requiring a machine-evaluable
grammar would be more reproducible and could prove exclusivity before any convention reaches a
prompt.

**Cheapest disconfirming experiment:** Create ten hand-labeled branch-selection fixtures by
paraphrasing SHQ-E1 evidence, including clear branch A, clear branch B, overlap, and no-match cases.
If the bounded selector chooses a wrong branch on any overlap/no-match case instead of clarifying,
or clarifies on more than one of the unambiguous cases, natural-language selection is not safe
enough and the predicate grammar should become mandatory.

The SHQ-E1 holdout evidence—multi-customer impact, rollback, return to baseline, and no permanent
remedy—should yield exactly one selection of its mitigated branch without clarification. A blend
with the report-only branch, or a correct-looking mapping without the selected branch and cited
evidence, should fail validation rather than pass the benchmark accidentally.

## 4. What are the convention budget and lifecycle defaults?

**Question:** What per-record token/branch limits, store quota, expiry policy, and supersession authority should conventions use?

**Recommended answer:** Use these initial defaults:

| Config key | Default | Behavior |
|---|---:|---|
| `memory.convention_max_tokens` | `384` | Count the deterministic rendered record, including its key, conditions, and every branch; quarantine the entire candidate on overflow and never truncate. |
| `memory.convention_max_branches` | `8` | Reject zero/one-branch protocols as non-protocols and quarantine records with more than eight branches; never split one protocol across entries. |
| `memory.convention_max_entries` | `256` | Apply a separate per-scope quota to `kind="convention"`; first remove expired/superseded records, then refuse admission and request review rather than evict or summarize an active record. |
| `memory.convention_default_ttl_days` | `180` | Set `expires_at` at promotion; expiry makes the record ineligible for retrieval until the same authority reissues or a human renews it. |

Pick 384 tokens because it is the midpoint of the researched 256–512 range, is more than three
times the 120-token cap that preserved one SHQ-E1 episodic lesson, and leaves room for both
conditions, all eight SHQ-E1 branch values, provenance, and an application rule without granting a
512-token prompt slot by default. These are convention-specific limits: they do not change
`memory.guideline_max_tokens` or the current 500-character guideline sanitizer. Convention
rendering must have its own atomic sanitizer and budget.

Only the same authenticated config issuer within the same scope, or a human approver, may set
`supersedes`; task/model/tool origins may propose but never activate it. The target must have the
same `(scope, key)`, the new version must differ, and activation is atomic: an unresolved conflict
quarantines the new record and leaves the old record active. Hit count, temporal-decay score, and
generic staleness thresholds neither expire nor supersede a convention.

**Strongest argument against:** A 180-day TTL creates operational churn and can silently remove a
stable protocol precisely because nobody needed to edit it; 384 tokens and eight branches may also
exclude legitimate decision tables. Conversely, 256 active conventions per scope can consume a
large prompt/search surface, so these numbers are guesses until a real convention corpus exists.

**Cheapest disconfirming experiment:** Deterministically serialize SHQ-E1 plus every candidate in a
small hand-audited seed corpus and report token count, branch count, and projected per-scope
occupancy—no model call is required. Any approved record above 384 tokens or eight branches, or a
normal scope projected to reach 256 active records within 180 days, disproves at least one default.

If these limits are right, the stored and rendered SHQ-E1 record should contain both full branches
without ellipsis. The holdout should retrieve one atomic convention and produce the four branch-B
values; a stored full record followed by render-time truncation is a benchmark failure, not a
capacity success.

## 5. What evidence unlocks `core` placement?

**Question:** Is authorized declaration enough for scoped retrieval, and what recurrence, branch-coverage, offline, and live gates should precede always-on prompt placement?

**Recommended answer:** Authorized declaration is sufficient for `tier="retrieve"`, but never for
`tier="core"`. Add these defaults: `memory.convention_core_enabled: false`,
`memory.convention_core_min_sessions: 5`,
`memory.convention_core_require_all_branches: true`,
`memory.convention_core_offline_min_delta_pp: 5`, and
`memory.convention_core_canary_min_uses: 100`. Core eligibility requires all of the following:

1. The current version is authorized, unexpired, conflict-free, and has no contradiction.
2. At least five distinct future-task sessions used it successfully; declaration/training sessions and repeated retrievals in one session do not count.
3. Every branch has at least one successful, independently scored application and zero observed branch-selection safety failures.
4. A pre-registered offline comparison over at least 30 applicable cases shows at least a 5 percentage-point task-success gain for core versus scoped retrieval, with zero increase in forbidden-output or wrong-branch count.
5. A 10% live canary reaches 100 applicable uses with zero new forbidden-output, wrong-branch, or ambiguity-bypass events; a human then explicitly changes the tier to `core`.

The gates are necessary, not an automatic promotion formula. Recurrence supplies usefulness
evidence but never authority, and the current `tiering_enabled` preference must not silently turn
into always-on convention injection. Disabling core must leave ordinary scoped retrieval working.

**Strongest argument against:** Requiring every branch to be observed can indefinitely block core
for sound policies with rare emergency branches, while 30 offline cases and 100 live uses may be
unavailable to small deployments. The five-point uplift threshold also treats a prompt-placement
decision like a product KPI even when the real value is preventing one rare, severe miss.

**Cheapest disconfirming experiment:** After an exact-key retrieval fixture passes, run the SHQ-E1
holdout and its three existing evolution variants once with ordinary scoped retrieval and once
with the same eligible convention forced into the core slot, keeping the record identical. If
scoped retrieval still misses SHQ-E1 while forced core consistently supplies it and fixes the four
structured fields, authorized retrieval is not enough and the core gate is too strict.

For the present SHQ-E1 proving ground, the expected result is a pass through `tier="retrieve"`,
with no core convention injected: the training and holdout observations cover only two sessions
and do not satisfy the proposed core gates. If SHQ-E1 passes only when forced into core, that is
evidence against the retrieval implementation or this answer—not justification to relabel the
record after the fact.

## Files reread

- `docs/design/convention-mining-research-2026-07-22.md` (full)
- `benchmarks/results/headroom-smoke-diagnosis-2026-07-22.md` (full)
- `benchmarks/datasets/company_memory_experiments.yaml` (SHQ-E1 case and evolution variants)
- `src/fabri/memory/schema.py` (full)
- `src/fabri/memory/verification.py` (full)
- `src/fabri/orchestrator/retrieval.py` (retrieval configuration, filtering, ranking, rendering, and sanitization)
- `src/fabri/orchestrator/action_execution.py` (full)
- `src/fabri/config.py` (memory defaults and related configuration wiring)
- `/Users/rushour0/.codex/skills/i-have-adhd/SKILL.md` (required interaction instructions)

## Least-confident answer

I am least confident in answer 5's numerical `core` gates. Default-off, human promotion, full
branch coverage, and zero safety regressions match fabri's posture, but the exact five-session,
30-case, five-percentage-point, and 100-canary-use thresholds have no convention-specific run data
behind them; they are deliberately conservative starting values. SHQ-E1 can test whether scoped
retrieval makes core unnecessary, but it cannot establish thresholds for rare branches or small
deployments, so those numbers should be the first defaults revisited once shadow-mode convention
telemetry exists.
