# Memory tiering and memory-as-action patterns for Fabri

Research date: 2026-07-22. This is a design research note, not an implementation plan. “Core” below means always-on prompt content; “retrievable” means stored outside the prompt and selected for a run; “action” means a typed, executable resolution, not prose that asks the model to remember to act.

## Q1. Prompt vs memory vs drop

The strongest systems do not use one score to decide everything. They separate *placement* from *retrieval* and make the scarce, always-visible tier deliberately small:

| System | What is always in context | What is retrieved | What is consolidated or omitted | Design lesson for Fabri |
|---|---|---|---|---|
| MemGPT / Letta | Bounded core-memory blocks are persistent and always visible. Letta recommends them for small, important facts and guidelines. | Recall/archival memory is searched through tools; Letta recommends archival or external RAG for less-important or large material. | Paging and summarization move material out of the limited context rather than treating the full history as prompt. | “Core” is an explicit admission decision, not merely the top result of vector search. |
| ExpeL | Compact, distilled insights are supplied to the agent. | Relevant successful/failed experiences (trajectories) are recalled for the current task. | Raw experience is valuable as evidence but need not occupy every prompt. | Keep generalized rules and case evidence separate; retrieve the latter only when the task matches. |
| Generative Agents | Current observations/plans and selected memories enter context. | A memory stream is ranked using recency, relevance, and an LLM-assigned importance score. Accumulated importance also triggers higher-level reflection. | The paper stores the stream; importance gates retrieval/reflection more clearly than deletion. It is therefore evidence for *selection*, not an automatic-drop policy. | Add value/importance to relevance and recency, but do not equate a high embedding score with lasting value. |
| CoALA | Working memory is the active decision context. | Long-term episodic (experiences), semantic (facts), and procedural (how to act) memories are retrieved into working memory. | The taxonomy separates memory content by function; it does not prescribe one universal eviction formula. | A lesson, a prior run, and an executable procedure should be different record types with different policies. |

These patterns are documented in the [MemGPT paper](https://arxiv.org/abs/2310.08560), [Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy), [ExpeL](https://arxiv.org/abs/2308.10144), [Generative Agents](https://arxiv.org/abs/2304.03442), and [CoALA](https://arxiv.org/abs/2309.02427). The newer A-MEM work adds structured note attributes, links, and memory evolution ([A-MEM](https://arxiv.org/abs/2502.12110)); Mem0 extracts/consolidates salient conversational facts and reports a retrieval-based alternative to full history ([Mem0](https://arxiv.org/abs/2504.19413)); Graphiti models facts and their validity over time ([Graphiti overview](https://www.getzep.com/platform/graphiti/)). These are useful secondary patterns, but they are mostly chat/personal-memory systems, not evidence that their policies optimize an autonomous work agent.

For Fabri, use a four-way admission decision:

| Destination | Admission rule | Examples |
|---|---|---|
| Always-on core | Verified, high-severity, broadly applicable across tasks, repeatedly useful, cheap to state, and not safely enforceable in code/config. | “Never publish a result that fails the frozen rubric.” |
| Retrieved declarative memory | Verified or explicitly marked provisional; relevant only to certain task/tool/domain signatures; useful as guidance or evidence. | “For this research agency, truncation commonly appears in specialist generations.” |
| Executable action memory | Verified resolution with typed preconditions, bounded parameters, postcondition checks, and rollback/stop behavior. | Revenue Ops scoped `max_tokens` override. |
| Quarantine/drop | Contradicted, redundant, non-actionable, generic, unverified high-risk, stale beyond its validity, or expected value below its prompt/retrieval cost. Keep quarantine long enough to audit classifier mistakes; do not inject it. | Generic success summaries, one-off stylistic observations, or a fix whose causal link was never verified. |

This is stricter than Fabri’s current `verification: any|verified` retrieval gate: verification answers “may this be trusted?”, while tiering answers “is this valuable enough, and where?” It also respects the existing bounds (`top_k=5`, 500 characters per injected entry): the problem is slot quality, not unbounded prompt growth.

**Fabri recommendation:** Add an explicit `tier = core | retrieve | action | quarantine | drop` decision, with core requiring verified evidence plus high severity/generality/value-per-token. Treat relevance, recency, and recurrence as features—not as proxies for value—and keep chat-memory products as secondary design input rather than validation of the policy.

## Q2. Actionable/procedural memory: automatically applying a past resolution

There are three progressively stronger precedents:

- [Reflexion](https://arxiv.org/abs/2303.11366) stores verbal self-feedback in episodic memory and conditions later trials on it. This improves future choice but remains prompt-mediated: the model may ignore or misapply the text.
- [Agent Workflow Memory (AWM)](https://arxiv.org/abs/2409.07429) induces reusable workflows from trajectories and selectively supplies them for later tasks. It is the closest published analogue to learning a reusable multi-step resolution, although the workflow still guides generation rather than being an independently governed transaction.
- [Voyager](https://arxiv.org/abs/2305.16291) stores successful skills as executable code, retrieves them for new tasks, executes them in the environment, and refines programs using execution errors and self-verification. It is the clearest proof of the store → retrieve → run skill-library pattern. [Toolformer](https://arxiv.org/abs/2302.04761) separately demonstrates the tool-call contract: decide which API to call, when, with what arguments, then incorporate its result.

Fabri should store an action memory as a typed workflow, not arbitrary mined code:

```yaml
problem_signature: {structured stable fields plus a canonical fingerprint}
scope: {company, agency, role, provider/model, config generation}
preconditions: [machine-checkable predicates]
steps:
  - capability: set_role_config_override
    args_template: {role: researcher, max_tokens: 2048}
  - capability: set_role_config_override
    args_template: {role: writer, max_tokens: 2048}
postconditions: [resolved config is 2048, training completes without truncation]
rollback: restore prior scoped override
evidence: {source sessions, verification verdict, successful replays}
policy: {idempotent, max_attempts, approval class, expiry/version}
```

The executor should resolve logical `capability` names to registered Fabri tools, validate all preconditions, run through the existing tool dispatcher, verify postconditions, and record success/failure. It should never `eval` mined text or let a memory choose unrestricted arguments. High-blast-radius or irreversible workflows require approval; low-risk, verified, scoped, reversible actions may auto-run.

**Revenue Ops worked example.** The stored signature says: training phase; `market-research-brief`; roles `researcher` and `writer`; configured `max_tokens=768`; first completion truncates; one-shot retry at 1536 also truncates; terminal `LLMError` / failed required delegation. Its verified resolution is *not* “try more tokens” prose. It is a scoped, idempotent action that writes a Revenue-Ops-only override of 2048 for both roles, confirms the compiled values, reruns training, and accepts the action only if training succeeds without truncation. It must refuse to mutate the shared agency because seven companies depend on it.

**Fabri recommendation:** Introduce a first-class `ActionMemory` record and a governed resolution executor. Mine candidate workflows from successful recovery trajectories, but promote them to auto-runnable only after deterministic verification; execute typed tool calls through Fabri’s normal registry and retain prose reflection only as supporting evidence.

## Q3. Recurrence matching and bounding false application

Semantic similarity is useful for candidate recall but is too weak to authorize a fix. Mature error grouping starts with stable structure: Sentry fingerprints errors from stack trace, exception, and message, and only then uses embeddings to merge semantically similar issues within a threshold ([Sentry issue grouping](https://docs.sentry.io/concepts/data-management/event-grouping/)); GitHub code scanning likewise uses stable partial fingerprints across runs to suppress duplicate alerts ([SARIF fingerprinting](https://docs.github.com/en/enterprise-cloud%40latest/code-security/reference/code-scanning/sarif-support-for-code-scanning)). Classical case-based reasoning also warns that the most similar case is not necessarily the most *adaptable* one ([adaptation-guided retrieval](https://www.sciencedirect.com/science/article/pii/S0004370298000599)).

Use a cascade:

1. **Canonicalize telemetry.** Remove run IDs, timestamps, prices, paths outside the scoped component, and natural-language noise. Preserve error class, phase, agency/role, provider/model family, retry behavior, finish reason, relevant configuration values, tool/capability, and failing postcondition.
2. **Exact fingerprint first.** Hash the stable structured fields. Exact fingerprint plus satisfied action preconditions is the highest-confidence match.
3. **Structured near-match second.** Weighted field comparison may retrieve candidates, but designated hard fields must match. For a configuration action, scope, role, failure mode, and current value/range are hard fields.
4. **Embedding recall last.** Embed the normalized problem narrative to find candidates missed by schema drift. Embedding similarity can nominate a case; it cannot authorize execution.
5. **Applicability check.** Evaluate the stored action’s preconditions against current state and calculate an `apply_confidence` distinct from retrieval relevance.

Bound false application operationally:

- auto-apply only `verified` actions with exact or high structured match, bounded scope, idempotence, and a verifier;
- dry-run or read current state first; use compare-and-set so stale assumptions fail closed;
- canary the smallest unit, verify, then continue; stop on unchanged/worse error signature;
- retain the prior value for rollback and impose attempt/cost/rate limits;
- require approval for destructive, externally communicative, compliance-sensitive, or broad-scope changes;
- record retrieved/matched/applied/verified/rolled-back counts and estimate precision from reviewed false applies.

**Revenue Ops worked example.** Canonical fields include `phase=training`, `agency=market-research-brief`, `roles={researcher,writer}`, `error=LLMError`, `cause=truncation`, `configured_cap=768`, and `retry_cap=1536`. A repeat with different session IDs matches. A timeout, a different agency, or a role already at 2048 does not. A semantically similar “writer output was short” event may retrieve the case but fails the hard preconditions and receives no action.

**Fabri recommendation:** Implement deterministic error/config fingerprints plus a structured applicability predicate; use embeddings only for recall. Track false-apply precision explicitly and default to “retrieve as text/no action” whenever a hard field, precondition, verifier, or rollback path is missing.

## Q4. Cost/quality evidence

**Null direct answer:** I did not find strong, controlled evidence that the exact policy “small always-on agent core + retrieved-on-demand remainder” beats “all lessons in context” for autonomous work agents. MemGPT motivates virtual-memory paging and evaluates long-document/chat tasks, but does not isolate Fabri’s proposed tier classifier. ExpeL, Voyager, and AWM show benefits from selected experiences/workflows, but their baselines and interventions bundle several changes.

The closest measured proxies are:

- Mem0 compares its extracted/retrieved memory against full conversation context on LoCoMo and reports better memory-task scores together with more than 90% lower token cost and 91% lower p95 latency ([Mem0 paper](https://arxiv.org/abs/2504.19413)). This is unusually close, but it is conversational memory, not a controlled core-vs-remainder agent study, and extraction/retrieval are bundled.
- “Lost in the Middle” finds that accuracy depends strongly on where relevant evidence appears; in its open-domain QA case study, increasing retrieved documents from 20 to 50 produced only about 1–1.5% improvement while recall continued rising ([paper](https://arxiv.org/abs/2307.03172)). This supports limiting distractors, not a particular memory tier algorithm.
- Controlled GSM-IC experiments find dramatic accuracy drops after adding irrelevant sentences ([irrelevant-context paper](https://arxiv.org/abs/2302.00093)). Again, this is a distraction proxy rather than agent-memory evidence.
- A direct RAG-vs-long-context study finds the choice is task/model-dependent and proposes a hybrid rather than a universal winner ([RAG or long context](https://arxiv.org/abs/2407.16833)).

Fabri’s own 2026-07-22 result is therefore more decision-relevant than extrapolating a universal win: the one clean company was null (sign-test p=1.0, n=8); Support HQ’s second generation cost more; the 3×1 Reliability result was suggestive, not conclusive; and the control is not clean for two companies. The “low-value lessons crowd capped slots” explanation remains a hypothesis to test.

**Fabri recommendation:** Do not claim tiering improves quality yet. Ship it behind an evaluation flag and compare bounded-current retrieval against value-tiered retrieval on clean no-memory controls, measuring rubric pass rate, forbidden hits, retries, input tokens, cost, retrieval precision, and action success/false-apply rate.

## Q5. Auto-fixable failure classes in early lead generation

| Failure class | Detectable signature | Bounded automatic resolution | Mandatory guard |
|---|---|---|---|
| Bad/stale enrichment | Missing domain/title/seniority, job-change marker, conflicting providers, unverified/catch-all email | Re-run a provider waterfall; prefer newest corroborated fields; re-verify email; quarantine unresolved records | Do not invent fields or send to unverified addresses. Apollo exposes verified/unverified/update-required/catch-all states ([email status](https://knowledge.apollo.io/hc/en-us/articles/4423314404621-Email-Status-Overview)). |
| ICP mismatch | Required firmographic/role/trigger predicates fail or score falls below threshold | Re-enrich missing predicates, recompute score, remove from sequence or route to nurture/self-serve | Never “repair” a mismatch by relaxing ICP silently. |
| Duplicate lead/account | Normalized email/domain/CRM ID or fuzzy entity rule collides | Upsert/merge according to CRM policy; preserve activity/owner; prevent second sequence enrollment | Ambiguous pairs require review. HubSpot deduplicates contacts by email and companies by domain and supports reviewable rules ([HubSpot deduplication](https://knowledge.hubspot.com/records/deduplication-of-records)). |
| Malformed or hallucinated outreach | Missing fields, unresolved template tokens, invalid URL, unsupported factual claim, wrong locale/tone, excessive length | Regenerate only the bad field from approved evidence; validate against an output schema and lint rules | Never send merely because generation succeeded; review a sample before scale. Clay explicitly calls out normalization-before-scoring and reviewing AI outputs ([outbound guide](https://www.clay.com/guides/how-to-automate-outbound)). |
| Wrong routing/ownership | Territory, segment, timezone, or owner differs from current CRM policy | Recompute routing and update the draft/task before enrollment | Compare-and-set; do not overwrite a human change made after scoring. |
| Sequence-state error | Already contacted, replied, bounced, out-of-office, active in another sequence | Suppress duplicate enrollment, pause/resume at the allowed date, or create a rep task | One active campaign per policy; treat reply/opt-out as a stop signal. |
| Compliance/opt-out | Suppression-list match, missing lawful-basis/region data, absent unsubscribe/address, prior objection | Suppress globally, add required footer, or route for legal/manual review; never “fix” missing consent by inference | FTC guidance requires a clear opt-out and honoring it within 10 business days ([CAN-SPAM guide](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business)); UK ICO guidance says screen new B2B lists against suppression data and treats individual objections as absolute ([ICO B2B marketing](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/business-to-business-marketing/)). |

These are suitable action memories because they have observable preconditions and postconditions. Message-market fit, persuasion quality, and whether an account is strategically worth pursuing are not safe deterministic “fixes”; they need experimentation or human judgment.

**Fabri recommendation:** Start the action-memory pilot with enrichment refresh, dedupe/upsert, deterministic validation, suppression, and routing—high-frequency failures with machine-checkable outcomes. Keep actual sending approval-gated until false-apply and compliance behavior are measured.

## Q6. A practitioner-grounded early lead-generation process

The workflow below is a synthesis anchored in named practitioner write-ups rather than a single vendor funnel. Jason Bay recommends deriving a narrow ICP from closed-won/closed-lost patterns such as win rate, deal size, cycle, industry, company size, tech, geography, and triggers ([Bay’s ICP/disqualification write-up](https://www.linkedin.com/posts/jasondbay_disqualification-the-single-most-important-activity-7457460352935698432-AYIG)). Eric Nowoslawski shows a Clay workflow that advances founders only after five explicit ICP tests, then generates the email ([Nowoslawski workflow](https://www.linkedin.com/posts/outboundphd_this-is-quite-possibly-the-craziest-clay-activity-7140701920247066625-1hfM)). Josh Braun breaks the first email into trigger, trigger-question, release, and proof ([Braun’s write-up](https://www.linkedin.com/posts/josh-braun_lets-write-a-good-cold-email-step-by-activity-6980954903359668226-QlE4)). Clay’s end-to-end guide supplies the operational sequence from ICP through source list, enrichment, normalization, personalization, sending infrastructure, and sequencer launch ([Clay outbound guide](https://www.clay.com/guides/how-to-automate-outbound)).

| Step | Tool category | Example tools operators use | What a Fabri agent calls |
|---|---|---|---|
| 1. Define ICP | CRM analytics + intent data | Salesforce, HubSpot, Pipedrive; 6sense, Bombora | Query recent closed-won/lost and cycle/ACV data; summarize common firmographic, persona, and trigger predicates; save a versioned ICP policy for approval. |
| 2. Source accounts/contacts | Prospect database + professional network | LinkedIn Sales Navigator, Apollo, ZoomInfo, Clay | Search companies matching approved filters; find relevant buying roles; return source IDs and provenance, not a raw unaudited send list. |
| 3. Enrich | Multi-provider enrichment | Clay, Apollo, ZoomInfo, Clearbit / HubSpot Breeze Intelligence | Enrich company domain, industry, headcount, location, technology, role, seniority, and recent signals; retain provider and observed-at timestamps. |
| 4. Verify, normalize, dedupe, suppress | Email verification + CRM data quality + compliance | NeverBounce, ZeroBounce, Apollo verification; Salesforce/HubSpot matching; suppression list | Normalize domain/name/title, verify address, search CRM by stable identifiers, upsert or quarantine, and check opt-out/legal policy before scoring. |
| 5. Score and qualify | Rules/score engine + intent | Clay, CRM workflows, 6sense, Bombora | Evaluate hard exclusions first, then fit/intent score; attach reasons and route `qualified`, `nurture`, or `reject`. Never let missing data score as positive. |
| 6. Personalize | Evidence retrieval + structured generation | Clay AI/Functions, CRM activity, approved web research | Retrieve one timely trigger and approved proof; generate typed fields (`subject`, `opener`, `body`, `evidence_ids`); reject unsupported claims or unresolved tokens. |
| 7. First touch | Sales engagement / sequencer | Outreach, Salesloft, Instantly, Smartlead, HubSpot sequences | Create a draft or add the qualified, verified, unsuppressed lead to an approved sequence with campaign, sender, and idempotency key; require approval for send during pilot. |
| 8. Follow-up and stop | Sequencer + CRM | Outreach, Salesloft, Instantly, Smartlead, Salesforce/HubSpot/Pipedrive | Read reply/bounce/OOO/opt-out state; stop, pause, resume, or create a human task; log the touch and outcome back to CRM for ICP feedback. |

**Revenue Ops worked example.** Before this sales workflow starts, the same recurrence/action gate handles the deterministic infrastructure failure: it recognizes the 768→1536 double-truncation signature, applies the Revenue-Ops-only 2048 overrides to researcher and writer, verifies non-truncated training, and only then lets the lead-gen company execute steps 1–8. This is the distinction between repairing the agent’s capability and asking the broken agent to remember a guideline.

**Fabri recommendation:** Encode the eight steps as a vendor-neutral workflow of logical capabilities and explicit gates. Treat CRM as system of record, require provenance through enrichment/personalization, and make suppression/reply/bounce state authoritative stop conditions.

## Q7. MCP-first connector architecture and Fabri’s current surface

MCP supplies exactly the indirection Fabri needs: a client discovers typed tools with `tools/list` and invokes them with `tools/call`; a host can combine tools from several servers into one registry ([MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture), [tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)). Remote authorization is based on OAuth flows and protected-resource discovery in the current specification ([MCP authorization](https://modelcontextprotocol.io/docs/tutorials/security/authorization)). Public web capabilities also exist as MCP servers, including the maintained [Fetch server](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md) and [Brave Search server](https://github.com/brave/brave-search-mcp-server).

### What Fabri supports today

Fabri already has the core extension seam:

- JSON manifests in configured directories register arbitrary subprocess tools; calls use JSON stdin/stdout and normalize timeout/error/result handling ([manifest schema](../../src/fabri/tools/manifest_schema.py), [runner](../../src/fabri/tools/runner.py#L24), [registry](../../src/fabri/tools/registry.py#L20)). The bundled inventory includes file read/write/edit/list/search, shell/Python, fetch/web search, batch, ask-user, and subagent tools ([built-ins](../../src/fabri/tools/examples)).
- `ToolRegistry` also accepts in-process callables, and every normal model tool call reaches `tools.invoke(name, args)` in the existing loop ([registry dispatch](../../src/fabri/tools/registry.py#L55), [agent dispatch](../../src/fabri/core/agent.py#L1160)).
- `tools.mcp_servers` is already config-declared. At build time Fabri connects, initializes, calls `tools/list`, wraps each remote tool as `mcp_<server>_<tool>`, and registers a callable that forwards `tools/call` ([README config](../../README.md#L295), [runtime registration](../../src/fabri/runtime.py#L250), [MCP adapter](../../src/fabri/tools/mcp_client.py#L263)).

The limitation is compatibility, not absence. The current client supports stdio NDJSON and a custom plain JSON-RPC POST mode, uses a hard-coded 2024-11-05 protocol version, static headers/env auth, no OAuth flow, no Streamable HTTP/SSE, no tool-list pagination/change notifications, and no resources/prompts ([transport caveats](../../src/fabri/tools/mcp_client.py#L1)). Its HTTP transport therefore cannot directly consume many current vendor servers even though `mcp_servers` exists.

### Public sales MCP availability found in this pass

| Tool | Verified public/vendor MCP status on 2026-07-22 | Fabri implication now |
|---|---|---|
| HubSpot | Vendor-hosted remote MCP, CRM read/write, OAuth 2.1 + PKCE ([HubSpot docs](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server)) | Upgrade Streamable HTTP/OAuth or use a reviewed bridge; static-header POST is insufficient. |
| Salesforce | Vendor MCP offerings exist, including the Salesforce DX server and hosted MCP servers ([Salesforce DX MCP](https://github.com/salesforcecli/mcp), [hosted MCP overview](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/hosted-mcp-servers-overview.html)) | Confirm the selected server exposes the needed sales-object actions; then connect through standards-compliant remote auth. |
| Apollo | Native server at `https://mcp.apollo.io/mcp`, Streamable HTTP + OAuth 2.0 ([Apollo docs](https://docs.apollo.io/docs/apollo-mcp)) | Native Fabri HTTP client needs upgrade. |
| Clay | Vendor MCP exposes enrichment and workspace Functions with admin permissions/credit controls ([Clay docs](https://university.clay.com/docs/mcp-settings)) | Prefer calling Ops-approved Functions instead of exposing dozens of raw provider actions. |
| Pipedrive | Native MCP can find/create/update CRM objects under granted permissions ([Pipedrive docs](https://support.pipedrive.com/en/article/mcp)) | Treat as preferred CRM connector once auth/transport is compatible. |
| Outreach | Vendor MCP with OAuth 2.1/PKCE, Dynamic Client Registration, Streamable HTTP, and discoverable annotations ([Outreach docs](https://developers.outreach.io/mcp-server)) | Strong fit for sequencer actions and risk annotations after client upgrade. |
| Salesloft | Vendor announced and ships an MCP server for live pipeline/accounts/calls ([Salesloft](https://www.salesloft.com/platform/ai-agents)) | Verify the tenant/add-on and tool catalog before binding action memories. |
| Instantly | First-party material documents an Instantly MCP for campaigns, Unibox, CRM, and follow-up automation ([Instantly](https://instantly.ai/blog/mcp-server-sales-automation/)) | Verify endpoint/auth/tool catalog in the customer tenant before production use. |
| Smartlead | Vendor MCP exists but its documented transport is SSE-only and API-key based ([Smartlead docs](https://helpcenter.smartlead.ai/en/articles/300-smartlead-mcp-server)) | Current Fabri client cannot consume it directly; use a bridge or adapter until SSE is supported. |
| ZoomInfo, LinkedIn Sales Navigator, NeverBounce, ZeroBounce, 6sense, Bombora | No first-party public MCP server was confirmed in this pass **[UNVERIFIED]**. Community/aggregator servers may exist, but that is not equivalent to vendor support. | Build a thin, least-privilege custom adapter over the official API; use raw HTTP only for a narrow prototype. Do not automate LinkedIn via unofficial scraping endpoints. |

### Recommended layering

1. **MCP server (preferred):** vendor-supported or reviewed server; dynamic schema discovery; scoped auth; use when the tool catalog and transport are stable.
2. **Custom Fabri adapter:** a small manifest/callable exposing domain operations such as `enrich_contact`, `upsert_lead`, or `suppress_contact`; use when no trustworthy MCP exists or when a narrow business-policy façade is safer than a vendor’s raw surface.
3. **Raw HTTP tool:** last resort for a few stable endpoints or a prototype; it must still have a tight schema, secret reference, timeout/retry/idempotency policy, response normalization, and audit trail. Do not let action memory hold arbitrary URLs, headers, or methods.

The minimal registration surface should evolve from today’s `name + command/env` or `name + url/headers` to: `name`, `transport`, `endpoint|command`, `auth` (OAuth discovery/client/scopes/token-store reference or secret-header reference), `allowed_tools`, per-tool read/write/approval policy, timeout/rate/cost limits, and optional logical capability aliases. Secrets should be referenced, never embedded in memory or prompt.

**Fabri recommendation:** Keep the existing registry and MCP wrapping model, but upgrade the client to the current Streamable HTTP/OAuth flow, pagination and tool-change support, add allowlists/risk policy, and bind action memories to logical capabilities. Prefer vendor MCP, then a narrow custom adapter, then raw HTTP.

## Q8. Concrete Fabri design

### 1. Classifier inputs and timing

Compute transparent feature groups rather than one opaque LLM judgment:

| Feature | Examples | Effect |
|---|---|---|
| Severity / preventable harm | deterministic failure, compliance/safety leak, blocked deliverable, cost/retry waste | Raises retention/action priority; severe universal constraints may qualify for core. |
| Evidence / verification | contradicted, unverified, tool-verified, rubric-verified, successful replay count | Hard gate: contradicted drops; auto-action requires deterministic verification. |
| Recurrence / exposure | distinct sessions, affected companies/roles, opportunity count, recent recurrence | Raises expected value, but repeated noise alone cannot create core memory. |
| Generality and scope | one exact config vs agency-wide vs universal; required preconditions | Broad verified rules favor core; narrow fixes favor retrieval/action. |
| Actionability | typed fix exists, preconditions observable, idempotent, reversible, verifier available | Determines action eligibility. |
| Inject cost vs expected value | characters/tokens, displaced-slot cost, measured success/retry/cost delta | Favors concise core; demotes low-yield lessons even when relevant. |
| Freshness / validity | age, provider/config/schema version, explicit expiry, superseded-by link | Prevents stale actions and informs retrieval/eviction. |

Run this twice:

- **Mine-time admission:** deterministically extract signature, provenance, scope, candidate resolution, and cheap features. Reject obvious noise/duplicates; quarantine unverified action candidates; admit useful declarative candidates only to retrievable memory. Nothing becomes core or auto-action here.
- **Promote-time classification:** after cross-session recurrence and verification/replay data exist, recompute value and choose `core`, `retrieve`, `action`, `quarantine`, or `drop`. Demote/supersede when evidence changes. Store the score components and reason codes for audit.

### 2. Relationship to existing pruning

**Pick: sit upstream.** Do not replace `_eviction_score` initially. The current recency × hit-count eviction and store cap in [`pruning.py`](../../src/fabri/memory/pruning.py#L24) remain the last-resort storage safety valve. The new classifier prevents low-value entries from being admitted/promoted/injected in the first place, which directly targets the capped-slot hypothesis. Replacing eviction at the same time would confound the experiment; extending it with value/severity can follow only after classifier precision is measured. Protected/verified action records should live in a separately capped collection or record type, not gain immortality inside the prose store.

### 3. Retrieval and execution path

```text
trace/config state
  -> canonical problem signature
  -> exact/structured matcher (embedding recall only for candidates)
  -> declarative tier: bounded prompt retrieval
  -> action tier: policy + precondition gate
  -> resolve logical capabilities to ToolRegistry tools
  -> execute through the existing tool loop
  -> verify postcondition
  -> commit success metrics OR rollback/stop/quarantine
  -> continue the agent with current state and tool results
```

The action path must not convert the workflow back into prose and hope the LLM follows it. A small resolution executor should submit synthetic typed calls to the same dispatcher used by model-generated calls, so local tools, callable adapters, and MCP tools share timeouts, normalized results, traces, and policy. Results become normal tool observations; mined success/failure updates the action’s evidence and precision. Logical capability binding keeps a memory portable: `enrich_and_rescore_lead` can resolve to an approved Clay Function, Apollo MCP tools, or a custom adapter without changing the memory record.

This deliberately combines CoALA’s separation of procedural memory from working context ([CoALA](https://arxiv.org/abs/2309.02427)), Voyager’s verified executable-skill loop ([Voyager](https://arxiv.org/abs/2305.16291)), and MCP’s discoverable typed-tool contract ([MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)); none alone supplies the required production policy boundary.

### 4. End-to-end examples

**Revenue Ops truncation.** Mine a candidate from the deterministic 768 → retry 1536 → truncation/`LLMError` trace. Quarantine it until the scoped 2048 fix succeeds. At promote time, create an action with hard preconditions (company Revenue Ops; shared agency untouched; both compiled roles still 768; same truncation signature), typed calls to set Revenue-Ops role overrides, postcondition checks for resolved config and successful training, and rollback. On recurrence, exact fingerprint + preconditions authorize the action before training; after a successful replay, record avoided failure/cost. A different company may retrieve the case as evidence but cannot auto-apply because scope fails.

**Sales ICP mismatch.** A mined resolution says that leads with missing/changed firmographics were incorrectly rejected. The signature includes `stage=scoring`, missing/stale fields, ICP policy version, and no suppression hit. The action resolves `enrich_contact/company` to a Clay or Apollo MCP capability, re-verifies email, calls the approved scoring capability, and upserts the resulting score/reasons to HubSpot/Salesforce/Pipedrive. It never sends. If the lead now fails hard ICP predicates it is removed from sequence; if it passes, first-touch remains subject to the campaign’s approval policy. Thus the same tool loop closes learned lesson → recurrence match → real integration → verified state change.

### 5. Evaluation gate

Run shadow mode first: the matcher proposes an action but does not execute; reviewers label applicability. Then canary low-risk actions. Promotion to automatic execution should require minimum reviewed precision, zero severe false applies, successful verifier/rollback tests, and a measured net benefit. Compare against today’s bounded retrieval, not an artificial unbounded-prompt baseline.

**Fabri recommendation:** Build an upstream, auditable two-stage classifier; preserve current pruning during the experiment; add a separately governed `ActionMemory` and deterministic matcher/executor; and route both learned fixes and MCP/custom sales connectors through `ToolRegistry`. The first golden test should reproduce Revenue Ops detection, scoped 768→2048 application, verification, and refusal on a near-but-inapplicable case.

## References

- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) — hierarchical virtual-context/paging motivation and evaluations.
- [Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy) — always-visible blocks versus files, archival memory, and external RAG.
- [ExpeL: LLM Agents Are Experiential Learners](https://arxiv.org/abs/2308.10144) — extracted insights plus recalled past experiences at inference.
- [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442) — memory stream, importance/relevance/recency retrieval, and reflection.
- [Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427) — working, episodic, semantic, and procedural memory taxonomy.
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110) — attributed, linked, evolving memory notes.
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413) — consolidated/retrieved chat memory and full-context cost/latency proxy.
- [Graphiti overview](https://www.getzep.com/platform/graphiti/) — temporal facts, invalidation, and hybrid retrieval.
- [Reflexion](https://arxiv.org/abs/2303.11366) — reusable verbal feedback in episodic memory.
- [Agent Workflow Memory](https://arxiv.org/abs/2409.07429) — induction and selective reuse of workflows from trajectories.
- [Voyager](https://arxiv.org/abs/2305.16291) — executable skill library with retrieval, environment feedback, and self-verification.
- [Toolformer](https://arxiv.org/abs/2302.04761) — selecting APIs, arguments, and consuming tool results.
- [Sentry issue grouping](https://docs.sentry.io/concepts/data-management/event-grouping/) — stable fingerprints/stack structure plus thresholded semantic grouping.
- [GitHub SARIF fingerprinting](https://docs.github.com/en/enterprise-cloud%40latest/code-security/reference/code-scanning/sarif-support-for-code-scanning) — stable cross-run alert identity and duplicate suppression.
- [Adaptation-guided retrieval](https://www.sciencedirect.com/science/article/pii/S0004370298000599) — why problem similarity alone does not guarantee reusable solutions.
- [Lost in the Middle](https://arxiv.org/abs/2307.03172) — position/distractor effects and diminishing benefit from more retrieved documents.
- [Large Language Models Can Be Easily Distracted by Irrelevant Context](https://arxiv.org/abs/2302.00093) — measured reasoning degradation from irrelevant input.
- [Retrieval Augmented Generation or Long-Context LLMs?](https://arxiv.org/abs/2407.16833) — task/model-dependent RAG versus long-context comparison.
- [Jason Bay on disqualification and ICP narrowing](https://www.linkedin.com/posts/jasondbay_disqualification-the-single-most-important-activity-7457460352935698432-AYIG) — practitioner method for deriving ICP from won/lost outcomes.
- [Eric Nowoslawski’s scored Clay workflow](https://www.linkedin.com/posts/outboundphd_this-is-quite-possibly-the-craziest-clay-activity-7140701920247066625-1hfM) — explicit ICP gates before generated outreach.
- [Josh Braun’s step-by-step cold email](https://www.linkedin.com/posts/josh-braun_lets-write-a-good-cold-email-step-by-activity-6980954903359668226-QlE4) — trigger/question/release/proof first-touch structure.
- [Clay outbound automation guide](https://www.clay.com/guides/how-to-automate-outbound) — ICP-to-source-to-enrich-to-normalize-to-personalize-to-sequence workflow and failure modes.
- [Apollo email status overview](https://knowledge.apollo.io/hc/en-us/articles/4423314404621-Email-Status-Overview) — verified, unverified, update-required, unavailable, and catch-all states.
- [HubSpot record deduplication](https://knowledge.hubspot.com/records/deduplication-of-records) — email/domain dedupe and reviewable matching.
- [FTC CAN-SPAM compliance guide](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business) — opt-out and sender obligations.
- [ICO business-to-business marketing guidance](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/business-to-business-marketing/) — PECR/GDPR distinctions, objection rights, and suppression lists.
- [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture) — host/client/server model and primitive discovery.
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) — `tools/list`, schemas, `tools/call`, and human-in-the-loop guidance.
- [MCP authorization tutorial](https://modelcontextprotocol.io/docs/tutorials/security/authorization) — OAuth protected-resource and authorization-server discovery.
- [MCP Fetch server](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md) — public web-fetch MCP tool.
- [Brave Search MCP server](https://github.com/brave/brave-search-mcp-server) — public web-search MCP tools.
- [HubSpot remote MCP server](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server) — vendor-hosted CRM tools and OAuth/PKCE requirements.
- [Salesforce DX MCP server](https://github.com/salesforcecli/mcp) — vendor MCP implementation for Salesforce org operations.
- [Salesforce hosted MCP overview](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/hosted-mcp-servers-overview.html) — OAuth-based compatible-client access to Salesforce.
- [Apollo MCP](https://docs.apollo.io/docs/apollo-mcp) — native endpoint, Streamable HTTP, OAuth, permissions, and sales actions.
- [Clay MCP](https://university.clay.com/docs/mcp-settings) — workspace access, credit controls, enrichments, and Functions.
- [Pipedrive native MCP](https://support.pipedrive.com/en/article/mcp) — CRM retrieval and authorized record actions.
- [Outreach MCP server](https://developers.outreach.io/mcp-server) — OAuth/DCR, Streamable HTTP, annotations, and discoverable tool catalog.
- [Salesloft AI agents and MCP](https://www.salesloft.com/platform/ai-agents) — vendor MCP access to live revenue context.
- [Instantly MCP for sales automation](https://instantly.ai/blog/mcp-server-sales-automation/) — first-party description of campaign, reply, and CRM access.
- [Smartlead MCP server](https://helpcenter.smartlead.ai/en/articles/300-smartlead-mcp-server) — SSE-only transport, API-key setup, and exposed diagnostics/data.

## Could not verify online

- A first-party public MCP server for ZoomInfo, LinkedIn Sales Navigator, NeverBounce, ZeroBounce, 6sense, or Bombora was not confirmed. Treat the absence claim as **[UNVERIFIED]** and re-check vendor docs/tenant marketplaces before building an adapter.
- I found no controlled study isolating the exact Fabri hypothesis “small always-on core + retrieved remainder versus all lessons in prompt” for autonomous work agents. The cited measurements are proxies or bundled memory systems.
- I could not independently verify the private/frozen `agencies/market-research-brief` source from this checkout; the Revenue Ops 768→1536 failure and verified 2048 fix are taken from the trusted study facts and the checked-in benchmark report.
