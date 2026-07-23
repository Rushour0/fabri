# ScaleX-on-Fabri — Build Handoff (AUTHORITATIVE, synthesized + verified)

> This top section is the final Scope→Design→Verify→**Synthesize** output (13-agent
> run, 2026-07-21). It supersedes the pre-verification design draft further down on
> every point they disagree. **For fun, but planned to be built.** Separate from the
> benchmarking effort; inherits its `OPENAI_API_KEY`-only funding assumption.
> Hand to `codex exec` for implementation, or a fresh Claude instance for the
> research-flagged items.

## Key grounded findings (these changed the plan)

- **fabri already has an external-integration mechanism.** A subprocess `ToolManifest`
  contract + a working **MCP client and server** at `runtime.py:250-286`
  (`src/fabri/tools/{manifest_schema,registry,mcp_client,mcp_server}.py`). Most
  connectors are therefore **config, not code** (a `tools.mcp_servers` YAML block).
- **Studio run history is already durable** (`run_store.py`); only the compiled
  company-config tree is intentionally ephemeral. Earlier "temp" framing was wrong.
- **MVP crew reuse source is `fabri-rosters/agencies/bug-triage-crew` ONLY.** The
  `examples/agencies/` copy lacks the `__AGENCY_ROOT__/__RUN_FROM__/__AGENCY_SLUG__`
  placeholders and `agency.toml` (COGS/wedge metadata), so it silently breaks per-node
  isolation and Studio metadata. Never use the examples copy.
- **Connector gating work is small**: a secrets abstraction, one native comms tool
  (Slack), one MCP config (GitHub), one SSRF-hardened webhook primitive, and a
  guardrail/COGS gate before any real external *writes*.

## MVP walking skeleton (build first)

Holdco → **one** leaf crew (`bug-triage-crew`), proven end-to-end in Studio with real
COGS + a persisted memory entry, before any breadth.

```
companies/scalex/company.toml   (new; copy the support-hq template)
  ceo  (root holdco manager)
   └── bug_fix_crew  →  ../../agencies/bug-triage-crew   (fabri-rosters)
         ├── triager   ├── fixer   └── tester
```

**Acceptance:** `fabri studio --company companies/scalex/company.toml --home-root
~/.fabri-scalex`, submit one task against the crew's existing `workspace/store.py` +
`test_store.py` fixture; browser-verify org chart renders, live COGS ticks, and a new
memory entry appears under `~/.fabri-scalex/.fabri/`.

## Dependency-ordered build sequence

- **Wave 0 — FREE — docs/specs.** This handoff + codex/fresh-Claude briefs + MVP decision record.
- **Wave 1 — FREE — foundation.** (1) `ssrf-extract`: move `fetch_url.py`'s
  `_validate`/`_host_is_blocked` into importable `src/fabri/tools/security/ssrf.py`.
  (2) `B0-secrets`: `credential_store.py` + `secrets.py` resolver (`provider:handle`
  indirection). (3) `A2`: author `companies/scalex/company.toml` (leaf pinned to
  fabri-rosters); `fabri company compile` to check. (4) `A3/A5/A6`: read-only verification.
- **Wave 2 — SPEND (~$0.05–0.20, hard-capped $5) — walking skeleton.** ← **FIRST SPEND GATE.**
- **Wave 3 — FREE — Studio surface (no schema migration).** `c1-intake-form` (text+budget
  only; file upload deferred — no endpoint yet); `c2-cogs-invoice` (**owns**
  `billing.markup_pct`; `GET /invoices/<session_id>`); `c3a-portfolio-catalog-packaging`
  (`fabri studio --catalog`); `c4` no-op messaging fix.
- **Wave 4 — FREE (build) — connector primitives.** `B10-webhooks` (shared SSRF module),
  `B1-slack-tool` (reuse `slack_notify.py`), `B2-github-mcp` (YAML config, zero code),
  `B4-stripe` (test-mode, consumes `c2`'s field), `B9-email-smtp` (stdlib). **`C4-guardrail-gate`**
  (least-privilege + human-gate toggle + per-action cost/OTel trace) — hard-gates Wave 5 writes.
- **Wave 5 — SPEND (real external accounts) — live connectors.** `phase3-git-connector`
  (throwaway repo + scoped PAT, propose-don't-apply), `phase4-slack-connector` — both gated
  on `C4`. `phase2-self-improving-dashboard`: **spike 3–5 identical runs first**; build the
  cost-trend chart only if cost actually drops, else reframe around `reuse_rate` (matches the
  benchmarking finding that cost-reduction is unproven).
- **Wave 6 — FREE (build) — breadth.** Append a growth crew (reuse `ad-copy-crew`); catalog it.
- **Wave 7 — SPEND (unattended, largest risk; each behind its own gate).**
  `B14-egress-policy` → `B12/c6 scheduler-heartbeat` (hard budget ceiling + UI kill-switch
  mandatory) → `c3b-multitenant-portal` (`auth.db`+`runs.db` migration) → `A8` net-new
  automation crew → `phase7-parity` (governance/rolling-budget; own agentic-pm pass).

## Integrations roadmap (top of stack)

| # | integration | how in fabri | effort |
|---|---|---|---|
| 1 | Secrets/credential store | new `credential_store.py` (`provider:handle`) | M |
| 2 | Generic webhook POST | native tool + shared `security/ssrf.py` | S |
| 3 | Slack | native tool (reuse `slack_notify.py`) | S |
| 4 | GitHub | **MCP config only** (`tools.mcp_servers`), no code | S |
| 5 | Stripe (COGS→invoice) | native tool consuming `billing.markup_pct` | M |
| 6 | Email/SMTP | native `smtplib` tool | S |
| later | Postgres / Drive+Notion / CRM | MCP config (OAuth gap for Drive/HubSpot) | S–L |
| cut | Discord, Zapier/n8n | no current use case (Zapier consumes webhook/MCP) | — |

## Spend gate 1 (Wave 2 only)

Confirm `OPENAI_API_KEY` funded, then boot Studio + run one task against the seeded
fixture. ~$0.05–0.20, hard-capped $5 by `company.toml`'s `max_cost_usd`. Command:
`cd /Users/rushour0/gba/fabri-rosters && fabri studio --company companies/scalex/company.toml
--home-root ~/.fabri-scalex`, then submit one task via Composer / `POST /runs`. Every later
spend wave (Wave 5, Wave 7) needs its own separate go-ahead.

## Handoff split

- **→ codex exec (implementation):** Waves 1, 3, 4, 6 — deterministic file/tool/Studio edits,
  clear acceptance (`fabri company compile` green, Studio renders, tests pass). One file per delegation.
- **→ fresh Claude (research):** OAuth story for Drive/HubSpot MCP; `EgressPolicy` design;
  the governance/rolling-budget parity decomposition (`phase7`, wants an agentic-pm pass);
  the cost-vs-reuse dashboard spike analysis.
- **← human:** each spend gate; whether to enable the unattended scheduler.

---

# Appendix — pre-verification design draft (superseded where flagged)

> The sections below are the original Unit-D design draft, written before the verify
> + synthesis passes. Where they conflict with the authoritative plan above (reuse
> source, guardrail gate, `billing.markup_pct` ownership, `c1`/`c3` scoping), the plan
> above wins. Kept for its deeper positioning brief and per-item HOWs.

# ScaleX-on-fabri — build & handoff plan

> Status: proposal / for-fun build plan, NOT a committed roadmap item. Scoped
> 2026-07-21. Separate from the benchmarking initiative (BENCHMARKS.md,
> `benchmarks/`) — do not conflate acceptance bars.
>
> Grounds: this doc is Unit D (build sequence & handoff) of a 4-unit decision
> pass (A=company portfolio, B=integrations, C=Studio product surface,
> D=this doc). It also carries the positioning brief the prompt asked for.
> Citations point at real files in `/Users/rushour0/gba/fabri` and
> `/Users/rushour0/gba/fabri-rosters` — verify against current code before
> treating anything here as already shipped.

---

## 0. Positioning brief (grounds everything below)

**What fabri is**, per its own README: "the self-improving agent engine you
build products on" — agents mine their own run traces into memory so they
stop repeating mistakes, with published (not hand-waved) evidence: hybrid
retrieval 0.938 recall@5 vs dense 0.792; a 6-run pilot showing steps 5→4,
guideline reuse 0%→67%, cost ↓7.8% (README.md:7-25). It is Apache-2.0, "no
revenue threshold, no commercial license required" (README.md:33-38).

**The competitor it implicitly answers**: `paperclip.inc` /
`agencyenterprise/paperclip-ai` (MIT, ~20-70k GitHub stars) — a Node.js
control-plane + React dashboard that *orchestrates a company of external,
black-box agent CLIs* (Claude Code, Codex, Cursor, Gemini) on scheduled
heartbeats, with per-agent monthly budgets, org charts, and board-level
governance (approve/pause/rollback). Sourced in memory
[`paperclip-vs-fabri-dashboard`], refining [`paperclip-inc-teardown`].

**The crux difference**: paperclip is a control plane that *schedules and
governs* agents it cannot see inside; fabri is the engine that *runs* the
agent loop and *improves it* — agents get measurably cheaper and better at
a company's actual work, because it owns the loop, not just the calendar.
Paperclip caps spend; fabri reduces it and reports real per-run COGS.

**Chosen counter-wedge** (a prior session's explicit decision, reused here):
do not compete on governance/org-chart theater. Lead with what paperclip
structurally cannot offer a black-box agent:

1. **Self-improving agencies** — trace → analyze → compress → dedup →
   promote → retrieve (README.md "Philosophy" diagram). A support crew that
   ran yesterday is measurably faster/cheaper today. This is the one claim
   competitors literally cannot make about a wrapped CLI.
2. **Honest per-run COGS** — every fabri agency reports real cost (own +
   sub-agent split, by-model, tokens, steps) already surfaced in Studio's
   `CostSummary.tsx`, not a monthly budget ceiling bolted on from outside.
   "COGS-as-invoice" is a believable startup mechanic *because* the number
   is already real, not simulated for the pitch.
3. **Open engine you build ON, not just operate** — Apache-2.0 core,
   embeddable, forkable; a "startup" built on fabri owns its engine instead
   of renting a SaaS control plane over agents it can't inspect.

**The pitch for a fabri-based automation-agency startup** ("ScaleX-on-fabri"):
*"A holding company of AI-native agencies — support, growth, dev-shop — each
one a fabri company that gets cheaper and better every week it runs, with
a real invoice behind every delivery instead of a subscription behind a
black box."* Who it's for: teams evaluating "hire an AI agency" vendors who
want to see the org chart, the actual run, and the actual cost — not a demo
video. Keep this light: it is a demo/portfolio framing exercise, not a
funding pitch — do not oversell the reliability evidence past what
BENCHMARKS.md actually shows (support-hq's 3/3 pass was overturned by a
10-replica 9/10 result — cite this honestly if the framing is ever shown
externally).

---

## 1. What Units A/B/C decided (for D to sequence against)

*(These are the decisive calls this pass grounds for A/B/C so the sequence
below has content to order — a fresh instance picking up A/B/C should treat
these as a starting proposal, not gospel, and re-verify against current
code.)*

**A — Portfolio.** Holding company `scalex-holding` (a `company.toml` at the
top, same shape as `fabri-rosters/companies/support-hq/company.toml`) with a
`ceo`/Chief-of-Staff root node and 3 sub-agencies as `[[node]]` entries
pointing at `agency = "../../agencies/<slug>"`:
- **`support-hq`-style** (already exists in fabri-rosters, reuse as-is) — an
  AI-automation/support agency. Lowest-risk, already benchmarked.
- **`growth-crew`** (new, compose from existing rosters agencies
  `ad-copy-crew` + `seo-brief-crew` + `market-research-brief` under one
  manager) — the growth/marketing agency.
- **`bug-triage-crew`** (already exists at
  `examples/agencies/bug-triage-crew/`, 4 agents: triager/fixer/tester +
  manager) — the dev-shop bug-fix crew.

**MVP pick: `support-hq` as the walking skeleton.** It already exists, is
already benchmarked (README evidence table), already has a `company.toml`,
and needs zero new agent authoring — only Studio wiring + a client-intake
skin. Building the *new* growth-crew or wiring a 3-company holding structure
is Phase 2, after one company is proven end-to-end in Studio.

**B — Integrations, decisive top picks** (full category sweep is B's job;
these are the ones D sequences first because they unlock a believable
"startup," grounded in `src/fabri/tools/` — the tool contract is a JSON
manifest next to a subprocess executable speaking `{ok, error?, result?}`
over stdin/stdout, per `docs/roadmap/connectors.md` and
`src/fabri/tools/registry.py`; MCP client/server already exist at
`src/fabri/tools/mcp_client.py` / `mcp_server.py`, so "MCP servers" is a
real option today, not aspirational):

1. **Git/GitHub connector (native tool, effort M)** — clone → branch →
   commit → push → open PR, "propose don't apply" default, through the
   existing sandbox (`$FABRI_SANDBOX_ROOT`). This is `docs/roadmap/connectors.md`
   card **C2**, already scoped as the walking skeleton of the whole
   connectors track. Priority: build first — it makes the dev-shop agency
   real (PRs against an actual repo, not a sandbox toy).
2. **Slack connector (native tool or MCP, effort M)** — read
   channel/thread + post reply; reuses the existing ask-user primitive
   (Track A) so a clarifying question can route to a Slack thread instead
   of Studio's UI. Card **C3**. Priority: second — it makes client-intake
   and status updates feel like a real agency, not a webhook oddity.
3. **Credential store (native, effort S, blocks 1 & 2)** — a
   `CredentialStore` protocol, env-backed for this demo, resolved by named
   handle (`github:acme`, `slack:acme-eng`) so no raw secret sits in agency
   YAML. Card **C1**, explicitly "blocks everything else" per the roadmap
   doc. Build this *before* C2/C3, not after.
4. **Stripe / billing-as-COGS-invoice (native tool, effort S-M, later)** —
   turn the already-real per-run COGS number (`CostSummary.tsx`'s own/
   subagent/by-model breakdown) into a line-item Stripe invoice or a
   generated PDF. Deliberately sequenced *after* the connectors, because
   it's presentation on top of a number that already exists — pure
   differentiator polish, not a blocker.
5. **Scheduler/heartbeats (native, effort L, deferred)** — needed for an
   "always-on" agency claim (paperclip's core mechanic) but explicitly
   **not** part of the MVP walking skeleton; it's parity work, not
   wedge work (see memory `paperclip-vs-fabri-dashboard`, slice ordering
   "1-2 = where fabri wins; 3-5 = parity").

Everything else B will enumerate (email/SMTP, Discord, GitLab, Notion,
Airtable, Postgres, HubSpot, Zapier/n8n, generic webhooks) is real
category-sweep work for B's own deliverable — D does not re-litigate it,
only anchors the sequence on the 3 above because they are the ones the MVP
acceptance check (§4) actually needs.

**C — Studio surface, decisive top picks** (grounded in
`examples/studio/src/components/CostSummary.tsx`, `CompanyOrgChart.tsx`,
`FleetView.tsx`, `src/lib/companyActivity.ts`, and the service backend
`src/fabri/service/http_server.py` with its `GET /company`, `/catalog`,
`/agencies`, `/runs`, `/fleets`, `/questions` routes):

1. **Run-history persistence** — per memory
   [`paperclip-vs-fabri-dashboard`], this **already shipped** as PR #48:
   sqlite `RunStore` (`src/fabri/service/run_store.py`), `~/.fabri/serve`
   persistent-by-default home root, `GET /runs?agency=&limit=&offset=` and
   `GET /agencies` (which already returns `cost_per_run_series` +
   `reuse_series`). **D does not need to build this — verify it's still
   true on current `main`, then build on top of it.**
2. **Client-intake form (new, effort S)** — a Studio panel that POSTs to
   the existing `/runs` route (already accepts a new-run POST per
   `http_server.py:276`) with a task description; this is UI work only, no
   new backend route needed for the MVP.
3. **Self-improving dashboard (new, effort M, THE differentiator surface)**
   — a per-agency trend view rendering `GET /agencies`'s
   `cost_per_run_series` + `reuse_series` over time. This is explicitly
   named in memory as "the wedge paperclip structurally can't show" and
   already has its data contract shipped. Build this **right after** the
   walking skeleton, before COGS-invoice or client portal.
4. **COGS-as-invoice panel (new, effort S, reuses `CostSummary.tsx` data)**
   — later; cosmetic framing of a number that already exists.
5. **Multi-company switcher / client portal, SLA/status, scheduler UI** —
   explicitly **later**, gated on the scheduler/heartbeat backend (B item 5)
   which doesn't exist yet; sequencing these before the backend would be
   building UI with nothing behind it.

---

## 2. Dependency-ordered build sequence

```
Phase 0  Credential store (C1)                [codex, S]
           │
Phase 1  Walking skeleton: support-hq in Studio, real run, real COGS
           │  - verify support-hq company.toml still resolves under
           │    current `fabri serve` (fabri-rosters/companies/support-hq)
           │  - verify GET /agencies + /runs return live data on current
           │    main (build on PR #48's RunStore; do not re-build it)
           │  - client-intake form → POST /runs                [codex, S]
           │  - browser-verify a real run appears in Company view + COGS
           │    panel populates                                 [claude, verify]
           │
Phase 2  Self-improving dashboard (per-agency trend UI)         [codex, M]
           │  consumes GET /agencies unchanged — no backend rework
           │
Phase 3  Git/GitHub connector (C2, walking skeleton of Track C)  [codex, M]
           │  depends on Phase 0 (credential store)
           │  unlocks: bug-triage-crew opening a real PR, not a sandbox diff
           │
Phase 4  Slack connector (C3)                                    [codex, M]
           │  depends on Phase 0; reuses ask-user primitive (Track A)
           │  unlocks: client-facing status + intake via Slack thread
           │
Phase 5  3-company holding structure (growth-crew authored + scalex-holding
         company.toml wiring 3 sub-agencies under one CEO node)  [codex, M]
           │  depends on Phase 1 proving the single-company pattern works
           │
Phase 6  COGS-as-invoice panel (Stripe or generated-PDF line items)
           │  [codex, S-M] — pure presentation on Phase 1's already-real
           │  cost data
           │
Phase 7  (deferred, not in this build's scope) scheduler/heartbeats,
         rolling budgets, multi-tenant client portal, generic MCP surface
         adapters (Linear/Notion) — parity features, explicitly lower
         priority than the wedge features above.
```

Rationale for the ordering: Phase 0 blocks Phases 3-4 (no connector without a
credential handle). Phase 1 must land before Phase 2 (the dashboard has
nothing to trend without a real run happening in Studio). Phase 2 is placed
*before* the connectors deliberately — it's the cheapest, highest-leverage
proof of the actual differentiator (self-improvement visible over time), and
it doesn't need new backend work, only a chart against data that already
exists. Phases 3-4 (connectors) come next because they're what make the
*portfolio* (dev-shop, growth crew) do real-world work instead of sandbox
demos. Phase 5 (the second and third company) is sequenced *after* the
connectors partly land, so the new agencies get real integrations from
day one instead of being retrofitted. Phase 6 (billing) and Phase 7
(scheduler/governance parity) are explicitly last — cosmetic and
parity-with-paperclip respectively, not what makes this pitch different.

---

## 3. What to hand to whom

**To codex (implementation, one file/surface per delegated agent — no
main-loop direct edits per the multi-agent-delivery pattern):**
- Phase 0: `src/fabri/tools/credential_store.py` (new module) + a
  `credential_store.schema.json` manifest, env-backed resolution by
  `provider:handle` string, unit tests under `tests/`.
- Phase 1: Studio intake form component (new file under
  `examples/studio/src/components/`) wired to existing `POST /runs`; verify
  `support-hq/company.toml` still parses under current `fabri serve --company`
  invocation before touching UI.
- Phase 2: `examples/studio/src/components/AgencyTrend.tsx` (new) consuming
  `GET /agencies`'s existing `cost_per_run_series`/`reuse_series` fields —
  read `src/fabri/service/service.py`'s `list_agencies()` return shape
  first so the field names match exactly.
- Phase 3: `src/fabri/tools/git_connector.py` + manifest, "propose don't
  apply" (opens a branch + PR, never pushes to a protected branch
  directly), tested against a throwaway repo/worktree, not this repo's
  own branches.
- Phase 4: `src/fabri/tools/slack_connector.py` + manifest, reusing the
  existing ask-user routing plumbing (grep `ask_user` in
  `src/fabri/tools/` and `src/fabri/orchestrator/` first to find the
  exact hook point before adding a parallel path).
- Phase 5: new `fabri-rosters/agencies/growth-crew/` (compose from
  `ad-copy-crew`, `seo-brief-crew`, `market-research-brief` per rosters'
  existing `agency.schema.json`) + a `scalex-holding/company.toml` at
  `fabri-rosters/companies/` following `support-hq/company.toml`'s exact
  shape (`[company]` header with `max_cost_usd`/`memory_namespace`, then
  `[[node]]` blocks with `id`/`report_to`/`title`/either `prompt` for the
  root or `agency = "../../agencies/<slug>"` for a leaf).
- Phase 6: a new `src/fabri/tools/invoice_export.py` (or a pure-frontend
  render of `CostSummary.tsx`'s existing data as a printable invoice —
  prefer the frontend-only version first, it's strictly less new surface).

**To a fresh Claude research instance (further research, not
implementation):**
- Full category sweep for Unit B beyond the top-5 here (email/SMTP,
  Discord, GitLab, Notion, Airtable, Postgres, HubSpot/Salesforce,
  Zapier/n8n, generic webhooks) with the S/M/L/XL effort + native-vs-MCP
  call for each — this doc only anchors the 3 that gate the MVP.
- A real `agentic-pm` decomposition pass over `docs/roadmap/connectors.md`'s
  C1-C5 cards into acceptance-criteria'd vertical slices (the roadmap doc
  itself flags this as not yet done: "It needs a proper agentic-pm
  decomposition pass before any code lands").
- Verification that the paperclip-ai star count / feature claims in
  memory [`paperclip-vs-fabri-dashboard`] still hold if this framing is
  ever used outside an internal/fun context — that memory is dated
  2026-07-20 and paperclip is a fast-moving OSS project.

---

## 4. MVP acceptance check

The MVP is done when, on a single `fabri serve` invocation against
`fabri-rosters/companies/support-hq` (or the eventual `scalex-holding`):

1. A task submitted through a Studio client-intake form produces a real
   run — visible in Studio's Company view (org chart lights up, live
   COGS panel populates) — **not** a screenshot or mock.
2. `GET /agencies` for that company shows a non-empty
   `cost_per_run_series`/`reuse_series` after at least 2 runs of the same
   task shape, and the self-improving dashboard (Phase 2) renders it as a
   visible downward-cost / upward-reuse trend — the literal claim
   competitors can't make about a black-box wrapped agent.
3. The run's total cost shown in `CostSummary.tsx` matches what the
   engine actually billed (own + sub-agent split, by-model breakdown) —
   no synthetic/hardcoded number anywhere in the demo path.
4. (Post-connector milestone, not MVP-blocking) the bug-triage-crew agency
   opens one real PR against a designated throwaway repo through the git
   connector, using a named credential handle, with the diff visible in
   the Studio run's trace.

Gate check-in point: before declaring Phase 1 "done," run it through the
project's real build/test gate (per the standing `agentic-em` operating
default) — this doc sequences work, it does not replace verification.

---

## Sources cited

- `/Users/rushour0/gba/fabri/README.md` (positioning, evidence table,
  philosophy diagram, license)
- `/Users/rushour0/gba/fabri/docs/roadmap/connectors.md` (Track C cards
  C1-C5, tool contract, MCP option, guardrails)
- `/Users/rushour0/gba/fabri/docs/ROADMAP.md` (Track M/O/S/A/B context)
- `/Users/rushour0/gba/fabri/src/fabri/tools/` (`registry.py`,
  `mcp_client.py`, `mcp_server.py`, `agent_runner_tool.py` — tool contract
  + MCP support confirmed real, not aspirational)
- `/Users/rushour0/gba/fabri/src/fabri/service/http_server.py` (routes:
  `/runs`, `/agencies`, `/company`, `/catalog`, `/fleets`, `/questions`)
- `/Users/rushour0/gba/fabri/examples/studio/src/components/CostSummary.tsx`,
  `CompanyOrgChart.tsx`, `FleetView.tsx` (Studio surfaces that exist today)
- `/Users/rushour0/gba/fabri/examples/agencies/bug-triage-crew/` (existing
  4-agent dev-shop crew)
- `/Users/rushour0/gba/fabri-rosters/companies/support-hq/company.toml`
  (real holding/company config shape: `[company]` + `[[node]]`)
- `/Users/rushour0/gba/fabri-rosters/schema/agency.schema.json` (agency
  metadata schema: name/title/tagline/category/stats/cogs/wedge)
- `/Users/rushour0/gba/fabri-rosters/index.json` (rosters catalog —
  `ad-copy-crew`, `seo-brief-crew`, `market-research-brief` confirmed to
  exist for the proposed growth-crew composition)
- User memory `paperclip-inc-teardown` (2026-07-18) and
  `paperclip-vs-fabri-dashboard` (2026-07-20) — competitive framing,
  chosen counter-wedge, PR #48 (`RunStore`) status. Point-in-time notes —
  re-verify claims against current `main` before treating as fact.
- `BENCHMARKS.md` / `benchmarks/results/support-hq-setup-qualification-*`
  — honest caveat that support-hq's reliability evidence did NOT clear
  the 100% bar at 10-replica sample size; do not overstate this in any
  external-facing framing.
