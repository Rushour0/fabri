# Changelog

All notable changes land here, newest first. Versions follow PyPI
immutability: never reuse a version number; cut a new one for any change
that ships.

## 0.23.3

- Studio's **Settings surface is now visible signed-out**. It previously
  rendered only for an authenticated user, so a visitor had no way to learn that
  fabri connects to Slack, GitHub, and Linear at all — the capability was hidden
  behind the thing it was meant to sell. Each integration now shows its name,
  what it does, and the handoff it performs: where a task starts, what the agency
  does, and what lands back. Only the connect action is gated, and it opens the
  sign-in screen with integration-specific copy instead of the
  save-your-history pitch.
- Signed-out cards skip their authenticated API calls instead of rendering a
  failed fetch, and a server with no `GITHUB_APP_SLUG` now says the GitHub App
  is unconfigured rather than silently rendering no connect link.

## 0.23.2

- GitHub App auth accepts the private key as **inline PEM content**, not only a
  file path: if `FABRI_CRED_GITHUB_PRIVATE_KEY` contains `-----BEGIN` it is used
  directly (with literal `\n` expanded), otherwise it is treated as a path. This
  makes container/env-only deployments (e.g. Coolify) work without mounting a
  key file — the whole GitHub-App config can live in environment variables.

## 0.23.1

- `fabri repo run` can now act as the *connecting* Linear workspace: a new
  `--linear-workspace <id>` flag resolves `resolve_secret("linear:<id>")` — the
  per-workspace token stored by the "Connect Linear" OAuth flow — instead of the
  single-tenant `linear:default`. This closes the multi-tenant Linear loop:
  before this, connected workspace tokens were stored but never consumed by any
  run. Omitting the flag keeps the single-tenant default behavior unchanged.
  (GitHub multi-tenant already selects per-installation tokens via `--repo`;
  Slack multi-tenant routes inbound events per workspace. Linear now matches.)
- Also fixes the `setup_bots.py github --public` manifest (drop the
  auto-delivered `installation`/`installation_repositories` from
  `default_events`, and capture the manifest-conversion code on the localhost
  callback) — see PR #96.

## 0.23.0

- Multi-tenant **GitHub** and **Linear**, mirroring the 0.22.0 Slack pattern so
  any org can connect its own account/workspace and fabri acts with that
  tenant's own credentials. Single-tenant (`github:default` App-from-env,
  `linear:default` token-from-env) keeps working byte-for-byte.
  - **GitHub App** (per-installation): `GitHubInstallStore` (shared
    `installs.db`, `repos` as a JSON array, COALESCE-per-column upsert so the
    id-only `/github/setup` write never clobbers webhook data). `AppAuth` now
    mints and caches a token per `installation_id`, selected per repo via
    `installation_id_for_repo`. New routes: `GET /github/setup` (capture
    installation_id), `POST /github/webhook` (HMAC-SHA256 over the raw body,
    fail-closed; `installation` + `installation_repositories` lifecycle),
    `GET /github/installs`, `POST /github/installs/<id>/delete`,
    `GET /github/app-info`.
  - **Linear OAuth** (per-workspace): `LinearInstallStore` +
    `SqliteInstallCredentialStore` resolving `resolve_secret("linear:<ws>")`.
    New `linear_oauth.py` reuses the signed-state helpers. Routes:
    `GET /linear/install`, `GET /linear/oauth/callback` (verify-state →
    exchange → resolve workspace → store), `GET /linear/installs`,
    `POST /linear/installs/<id>/delete`.
  - `scripts/setup_bots.py github --public` / `linear --public` scaffold the
    distributable app + server env contract.
  - Studio gains "Connect GitHub" and "Connect Linear" settings tabs.
- Deploy: install stores live in `<home-root>/installs.db` — mount a persistent
  volume at the home-root. Tokens plaintext-at-rest (encryption-at-rest is a
  follow-up).

## 0.22.0

- Multi-tenant Slack: any workspace can now install the fabri Slack app via
  "Connect Slack" (OAuth v2 distribution) and fabri posts using **that
  workspace's own bot token**. A durable per-team SQLite install store
  (`service/install_store.py`, WAL) holds each `team_id`'s token; a
  `SqliteInstallCredentialStore` resolves `resolve_secret("slack:<team_id>")`
  transparently, so `slack_post`/`notify_slack` are unchanged. `slack:default`
  still falls through to the env token, so single-tenant deployments keep
  working byte-for-byte.
  - New server routes on the service HTTP server: `GET /slack/install`
    (signed-state CSRF → Slack authorize), `GET /slack/oauth/callback`
    (verify-state-before-exchange, fail closed, upsert the install),
    `GET /slack/installs` (auth-guarded, tokens never returned), and
    `POST /slack/installs/<team_id>/delete`.
  - Inbound events are tenant-aware: each event resolves its team's token;
    `app_uninstalled`/`tokens_revoked` delete the install. Signature
    verification is unchanged.
  - `scripts/setup_bots.py slack --public` scaffolds the distributable app
    (hosted redirect + event subscriptions with the correct scopes) and writes
    the server env contract (`FABRI_PUBLIC_BASE_URL`, `SLACK_CLIENT_ID`,
    `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`).
  - Studio gains a "Connect Slack" settings tab listing connected workspaces
    (names only) with a disconnect action.
- Deploy note: the install DB lives at `<home-root>/installs.db`; mount a
  persistent volume at the home-root or installs are lost on redeploy. Tokens
  are plaintext-at-rest in this version (encryption-at-rest is a follow-up).
  GitHub multi-tenant install and per-tenant outbound `ask_user` remain
  single-tenant / deferred.

## 0.21.0

- Add `fabri repo run --from-linear <ID> --repo <owner/name>`: a real
  software-workflow driver that turns a Linear issue into a GitHub pull
  request announced in Slack, driven by a fabri engineering agency running
  against a real git checkout. The orchestrator (`fabri/repo/run.py`) runs a
  ten-gate, fail-closed machine (resolve_creds → fetch_issue → clone → setup →
  agency_run → verified_tests → branch_push → open_pr → comment_linear →
  notify_slack); the `verified_tests` gate re-runs the target repo's own test
  command in the clone and its captured exit status is the sole authority for
  whether any external write happens. No PR, Slack post, or Linear comment is
  made unless the tests genuinely passed.
- New connectors, each SSRF-guarded and secret-store-backed:
  - Linear GraphQL client (`fabri/integrations/linear.py`): `fetch_issue`,
    `comment_issue`, `set_state`; fails closed on GraphQL `errors`.
  - GitHub auth seam (`fabri/repo/github_auth.py`): `AppAuth` mints and caches
    a short-lived installation token from a GitHub App id + key (RS256 JWT →
    installation access token), with a `PatAuth` fallback. PyJWT is an opt-in
    `fabri[repo-github]` extra, imported lazily only when App auth is used.
  - Multi-file branch push (`fabri/repo/git_local.py`): commits a real working
    tree and pushes to a bot-owned branch via a tokenized URL kept out of
    `.git/config`.
  - Slack `slack_post` builtin tool + a `notify_slack` post-run step.
- Every connector ships offline mock tests plus an env-gated live smoke
  (`@pytest.mark.live`, run only with `FABRI_LIVE_TESTS=1`). Bot/credential
  setup is covered by `scripts/setup_bots.py` and `docs/repo-run.md`.
- The bundled `bug-triage-crew` is hardened to operate on a real cloned
  checkout (fail-closed, anti-fabrication prompts; its tester's verdict gates
  the PR) and `runtime.build_tools` honours a `FABRI_SANDBOX_ROOT_OVERRIDE`.

## 0.19.4

- Revert the AGENT_MEMORY "hard output contract" steward templates (0.19.3).
  Live benchmark smokes showed the hardened wording did not increase block
  emission (0 of 2 training runs) and caused a structured-output regression:
  models embedded the memory block inside the JSON response value, failing
  schema validation in 3 of 4 arms. Convention capture moves engine-side
  (see docs/design/convention-mining-research-2026-07-22.md) instead of
  relying on model prompt compliance.

## 0.19.3 — 2026-07-22

### Fixed

- **Root managers silently dropped the `AGENT_MEMORY` block.** Live benchmark training runs
  (model `gpt-5.6-terra`) showed the block emitted in **0 of 4 runs**: when a task prompt demanded
  its own output shape (e.g. "return the ticket response and its compact decision record"), the
  model treated the memory-stewardship instructions as optional prose and resolved the conflict by
  dropping the block entirely. Both `_COMPANY_MEMORY_INSTRUCTIONS` and
  `_COMPANY_STRUCTURED_MEMORY_INSTRUCTIONS` in `company.py` now state plainly that the block is a
  **required** part of every successful final response regardless of the task's requested format —
  the task's format governs everything before the marker, the block always comes after (outside the
  JSON, for the structured variant), and a response missing it is incomplete. `INSIGHTS` guidance
  also now asks for the **complete** convention when a task establishes one for later work — every
  branch, with exact identifiers — not just the branch this run happened to take. Marker, field
  names, and the safety sentence (never store credentials, personal data, transient chatter, or
  unverified claims) are unchanged, so existing parsing and mining in `memory/output.py` keep
  working untouched.

## 0.19.2 — 2026-07-22

### Fixed

- **Rubric scoring was invalid in both directions.** Forbidden terms used a fixed 60-character
  negation lookbehind, so a negation cue one clause back was missed (false positive) *and* a
  negation cue in a **previous sentence** wrongly exempted a real hit (false negative). The window
  is now scoped to the enclosing sentence (capped at 400 chars). Required terms used naive
  substring matching, so a correct answer phrased differently scored as missing — e.g. "share
  **further customer-facing updates**" failed the required literal `further update`. Required
  matching is now order-preserving proximity within one sentence (≤4 intervening words, plural and
  hyphen/compound tolerant); single-word requirements keep their exact prior behavior, and the
  forbidden side is deliberately **not** loosened.
- **A "control" arm was never a no-memory control.** It started with no transported DB but still
  mined and retrieved its own lessons within the same run — contamination the study could only
  detect after the fact, which is why 2 of 3 companies had zero usable control arms.

### Added

- **Real memory off-switches.** `memory.mining_enabled` and `memory.retrieval_enabled` (both default
  `True`). Mining is gated at both `process_trace` call sites; retrieval short-circuits before any
  embedding or store query. The memory study writes both `False` into every compiled node for
  control arms — verified live at **0 guidelines retrieved**.
- **ActionMemory mining + shadow action detection.** `ingest_guideline` accepts `tier` and
  `resolution`; new `memory/action_mining.py` turns a run that hit `max_token_retries` into a typed
  `{problem_signature, scope, preconditions, steps, postconditions, rollback, evidence, policy}`
  resolution, stored as a quarantine-tier entry keyed by its recurrence fingerprint. `cmd_run` makes
  a **shadow** (log-only, never executed) `detect_proposed_actions` call behind
  `memory.memory_action_enabled` (default off). A test asserts a miner-produced candidate satisfies
  `recurrence.applicable()` and is refused against an already-fixed state.
- **`benchmarks/rescore_runs.py`.** Re-extracts each arm's real holdout output from its trace and
  re-scores it with the current rubric, reporting raw vs corrected without re-spending.

### Changed

- **Re-measured memory vs a true control (36 arms, $4.82).** No significant memory effect on quality
  or cost on any of three companies. Both the prior "memory hurts" headline and this study's own
  initial "+100pp memory wins" were scoring artifacts. See
  `benchmarks/results/memory-vs-true-control-2026-07-22.md`.

## 0.19.1 — 2026-07-22

### Added

- **Immutable company generations + quality-first evolution.** `fabri.benchmarks.company_evolution`
  snapshots a trained company's memory (hash-verified manifests), restores it into a fresh compile,
  trains a child generation, and compares parent vs child under a promotion gate (no rubric
  regression / forbidden hit / unaccounted cost; median cost ≤ 1.05× incumbent; ≥10% cheaper or
  ≥25% fewer retries; verified specialist retrieval on ≥2 variants).
- **Training-lesson verification.** `apply_training_verification` marks mined `success_pattern`
  lessons `rubric_verified` when the training run succeeded operationally; deterministic
  tool-failure lessons stay `tool_verified`. Verified-only retrieval consumes these verdicts.
- **Consolidated memory report.** `fabri.benchmarks.company_memory_report` aggregates a multi-company
  run into one JSON/Markdown report — separate attempted/finished/completed denominators, paired
  per-replica sign tests (not CI-overlap), retry categories, transport and specialist-retrieval rates.

### Fixed

- **Honest `training_success` accounting.** A truncated training run no longer reports
  `training_success: true`; the flag is derived from the failing phase, holdout/transport reasons
  route to `holdout_failure_reasons`, and `validate_memory_payload` rejects the contradiction.
- **Negation-aware rubric.** `score_text`'s forbidden-term check no longer false-positives on
  negations (e.g. "no evidence a fix was deployed"); a term counts only when it appears at least
  once un-negated.

## 0.19.0 — 2026-07-21

### Added

- **Automated company memory-vs-control study.** A new `fabri.benchmarks.company_memory_study`
  runner trains a roster company, then runs a fresh holdout twice — once with the learned
  SQLite memory copied into a clean compile, once with an empty control — reusing the setup
  probe's deterministic scoring. It publishes per-condition aggregates and memory−control
  deltas (rubric, cost, guidelines retrieved) with a self-generated reproducibility manifest,
  keeping prompts, traces, and raw output private. A `--retrieval-top-k` / `--retrieval-strategy`
  override lets a run sweep the compiled retrieval configuration.
- **Self-generated reproducibility manifests and schema-validated results.** The setup probe
  now emits its own manifest (roster revision, worktree-clean flag, company-source SHA-256,
  Fabri version) and validates the publication payload against a schema before writing, so
  `results.json` and `results.md` can no longer drift or be hand-curated.
- **Wider setup-probe candidate search.** The bounded search now covers per-role step and cost
  budgets, retrieval `top_k` and strategy, per-role model, delegation timeout, and max parallel
  spawns — each with no-op rejection so a config change with no runtime effect never spends a
  model run. Prompts, tools, and security policy stay outside the search.
- **Structured-field scoring and an optional frozen LLM judge.** Deterministic
  `expected.structured` subset assertions gate qualification; an opt-in, frozen judge (pinned
  model, temperature 0, versioned prompt, recorded in the manifest) provides an advisory
  semantic verdict that is kept out of qualification, winner selection, and the subject's cost.
- **Setup qualification for Reliability Labs and Revenue Ops** via `setup_probe` candidate blocks.
- **Remaining failure-path coverage** for the setup probe: compile failure, preflight failure,
  root-process timeout, malformed CLI JSON, missing trace, company cost-limit violation, the
  no-viable-setup status, and CLI exit codes.

### Changed

- **Manager delegations get a generous call timeout** so deep companies no longer spuriously
  time out waiting on a child's subtree; delegation timeout fields are validated at compile time.

### Fixed

- Setup-probe robustness: the reproducibility manifest degrades gracefully when the roster is
  not a git repository (missing/hung `git`), candidate knobs no longer spend on runtime-inert
  changes (subagent-shadowed budgets, non-spawning nodes), and structured-field scoring no
  longer treats `1` as `True`.

## 0.18.5 — 2026-07-20

### Added

- **Bounded company setup qualification.** A new dynamic-roster probe resolves
  companies through `FABRI_ROSTERS_ROOT`, recursively checks compiled
  delegation configs, runs isolated fresh replicas, rejects ineffective
  candidates before model spend, and publishes deterministic completion,
  rubric, cost, and selection aggregates while keeping raw traces private.
- **Reproducible company memory experiments.** Support HQ, Reliability Labs,
  and Revenue Ops now have training and holdout prompts, required/forbidden
  output assertions, retrieval expectations, and fresh compile/state isolation
  built from the existing `fabri company compile` and `fabri run` commands.
- **Guarded memory evaluation fixtures.** The recovery study records task
  precedence and separates operational completion from scoreable output so
  failed runs cannot be counted as benchmark failures or wins after the fact.

### Changed

- **The current task outranks retrieved memory.** Retrieved guidelines remain
  advisory context and cannot override the user's present instructions, with
  prompt construction and regression coverage enforcing that precedence.
- **Memory holdouts isolate mutable workspaces.** Training and holdout use fresh
  company compiles; only the learned SQLite database crosses into the memory
  holdout, while the control receives no trained database.

### Experiment result

- **Support HQ baseline qualified 3/3.** All three fresh incident-response runs
  completed their required delegation tree and passed the frozen deterministic
  rubric. Median recursively accounted cost was $0.020200; the released gate
  cost $0.060496. A proposed 256-token delegated-artifact floor was rejected as
  a no-op before model spend. This qualifies the setup for a later
  memory/control experiment; it is not a memory-benefit claim. Full method,
  failed hypotheses, and total research spend are in
  `benchmarks/results/support-hq-setup-qualification-2026-07-20.md`.

## 0.18.4 — 2026-07-20

### Changed

- **Studio no longer forces sign-up.** When authentication is enabled, visitors
  can browse the roster and run live conversations as guests. Signing in is
  requested only for saved history and account-scoped dashboards; owned runs
  retain their existing cross-user access controls.
- **Conversation history is now a sidebar.** Saved runs are grouped into
  conversations by thread and remain one click away without replacing the main
  work surface. Guests see the same rail with a clear, optional save-history
  action.
- **Password visibility control.** Login and sign-up now include an accessible
  show/hide-password button, plus a clear path to continue without saving.

## 0.18.3 — 2026-07-20

### Added

- **Durable company memory.** Compiled companies now have an explicit root
  institutional-memory collection, record a postmortem for every company task,
  and capture durable facts, decisions, insights, and open loops for later
  sessions. Catalog and Studio compilation anchor the SQLite database outside
  temporary build directories.

### Fixed

- **Roster learning survives new chats.** Studio no longer replaces an
  agency/company's configured memory collection with a per-thread collection.
  Conversation continuity remains in the transcript preamble while durable
  learning accumulates across sessions. Machine-readable `AGENT_MEMORY` blocks
  remain available to the trace miner but are hidden from human-facing results.

## 0.18.2 — 2026-07-20

### Added

- **Self-improvement integration coverage.** Deterministic, offline tests now
  cover both the retrieved-guideline path that changes an agent's next-run
  behavior and the `fabri repo open-pr` path that turns promoted lessons into
  a reviewable draft pull request without editing the checked-out config.
- **Reproducible COGS contract benchmark.** The benchmark record now includes
  a clearly labeled scripted integration result alongside the existing live
  session-N+1 experiment; it guards against an extra guided turn or a dropped
  learned-prompt update without presenting synthetic numbers as live-model
  performance.

## 0.18.1 — 2026-07-20

### Fixed

- **Studio auth hardening.** The catalog now requires a session whenever Studio
  authentication is enabled, and cross-user access to run events, results,
  answers, and cancellation remains explicitly regression-tested.

## 0.18.0 — 2026-07-20

### Studio — login + per-user conversation history

- **Opt-in email+password auth.** Sign in and see only your own runs/history.
  Off by default (`FABRI_AUTH_ENABLED=1` + `FABRI_AUTH_SECRET`). scrypt-hashed
  passwords, HMAC-signed **HttpOnly** session cookies, **per-run ownership
  checks** (no cross-user access), and per-user scoping of history / questions /
  fleets / agency cost totals. Login/signup UI in Studio; unchanged when off.

### `fabri repo` — open PRs + more providers

- **Open pull requests** from a run (not just issues), and **GitLab + Bitbucket**
  adapters alongside GitHub behind a common repo-provider interface.

## 0.17.0 — 2026-07-20

### `fabri repo` — self-improving, in your repo (GitHub)

- **`fabri repo suggest-prompt`** reads an agent's promoted `strategic` guidelines
  (lessons that recurred across ≥3 sessions) and opens a **deduplicated GitHub
  issue** proposing they be folded into the agent's prompt — the self-improving
  memory loop made visible and reviewable in the repo.
- **`fabri repo issue`** files/refreshes a deduplicated tracking issue (a hidden
  marker + list-before-create, so re-runs comment instead of spamming duplicates).
- **Easy setup, by design:** it runs in a GitHub Action from a single workflow
  file with only the built-in `GITHUB_TOKEN` — no model API key, no server. Live
  reference: [Rushour0/fabri-repo-demo](https://github.com/Rushour0/fabri-repo-demo)
  files its own self-improvement issue weekly.
- GitHub-only for now, token via `GITHUB_TOKEN` (Actions-native), stdlib only. See
  `docs/repo-agent.md` for a copy-paste GitHub Actions workflow.

## 0.16.4 — 2026-07-19

### Studio

- **UI polish pass — depth, blue accent, micro-motion.** Branded blue diamond
  logo; the primary CTA is the blue accent with a soft glow + hover lift; the
  composer input gets a blue focus ring; tabs get a hover fill + crisper active
  state; the task bubble, cards, and list rows gain layered shadows and a hover
  lift; the waiting "needs you" ask card breathes a slow blue glow. Honors
  `prefers-reduced-motion` (entrance/loop animations + lifts disabled).

## 0.16.3 — 2026-07-19

### Studio

- **Stable full-width frame — no more conversation "shrinking".** The app frame
  animated its width between tabs (720px ↔ 1440px), so switching to Conversation
  visibly shrank the whole frame. The frame is now full-width and stable on every
  tab; the conversation centers its own readable 768px column inside it, while
  list surfaces fill the frame.
- **Blue accent.** The single accent is now blue (`--accent #0d92f4`, `--accent-2
  #80c4e9`) instead of amber, matching the roster landing page.

## 0.16.2 — 2026-07-19

### Studio

- **Tabs are URL-tracked and deep-linkable.** Tab state now lives in the URL hash
  (`#conversation`, `#questions`, `#replay/<id>`) via a small `useHashRoute` hook
  (no router dependency), so tabs survive reloads and browser Back/Forward works.
- **Fixed the Questions width regression.** Switching to the Questions inbox
  snapped the layout back to the narrow conversation column; it now gets the full
  width like the other list surfaces (Roster/Company/Fleet/History). A replay
  inherits the width of the surface that opened it.

### Fixes

- **CI: install the `embeddings` extra so the memory/retrieval suite runs.** The
  test job installed only `.[dev,sqlite]`, so ~30 memory/retrieval/eval-gate tests
  errored on the missing `sentence-transformers` or silently no-op'd and failed
  downstream. CI now installs CPU-only torch + `.[dev,sqlite,embeddings]`.

### Internal

- Release workflow now cuts a GitHub Release (notes from this changelog) after the
  PyPI publish. Added `docs/roadmap/connectors.md` (Track C — agents that run
  inside a company's repos/Slack), scoping only.

## 0.16.1 — 2026-07-19

### Fixes

- **COGS: static specialists now roll into the parent total.** A parent run's
  `total_cost_usd` only counted `spawn_subagent` children — static
  `tools.agents[]` specialists (and, recursively, a company's whole org tree)
  were omitted, so multi-agent COGS were undercounted. They now roll up.
- **Studio: full-width roster/org-chart/fleet surfaces** (the conversation stays
  a narrow column) so the catalog and org-charts aren't cramped.

## 0.16.0 — 2026-07-19

### Studio catalog mode — browse a roster and run any agency/company

- **`fabri studio --catalog <rosters-dir>`** turns Studio into a catalog-first
  control plane: it pre-installs every agency and compiles every company from a
  roster (e.g. a checkout of `fabri-rosters`), serves `GET /catalog`, and each
  run targets the entry you pick (`catalog_ref` on `POST /runs`).
- Studio gets a **Roster** home surface — agency + company cards, each with a
  Run button; picking one shows a "Running <name>" banner, and companies drive
  the multi-level org-chart. A run of a multi-agent agency/company shows its
  sub-agents and their live per-agent COGS.

## 0.15.1 — 2026-07-19

- Base installs are now lean: `sentence-transformers` is optional. Use
  `pip install "fabri[embeddings]"` to enable memory retrieval and learning.

## 0.15.0 — 2026-07-18

### Human-in-the-loop routing + an installable agency/company layer

- **`fabri new agency --from <source>`** — scaffold an agency from a registry
  directory: a local path or `gh:owner/repo/subpath[@ref]`. Reuses the bundled
  templates' `__AGENCY_ROOT__/__AGENCY_SLUG__/__RUN_FROM__` placeholder contract,
  so each install gets its own memory collection. Feeds the separate
  [`fabri-rosters`](https://github.com/Rushour0/fabri-rosters) catalog.
- **`fabri company compile <company.toml>`** — a first-class *company*: a flat
  `company.toml` with `report_to` edges compiles to a nested tree of agent
  configs (no runtime change — fabri already recurses through `tools.agents[]`).
  Validated as a single-rooted tree; leaf agencies install from the registry.
- **`fabri studio --company <company.toml>`** + **`GET /company`** — serve a whole
  multi-level org in Studio; the Company tab draws its org chart (ceo → VPs →
  crews) and overlays each node's live status + real per-node COGS during a run.
- **Human-in-the-loop, dashboard-first.** A new Studio **Questions** inbox
  collects every pending `ask_user` question across all live runs (`GET
  /questions`), answerable inline — the run resumes on answer. Optional
  `routing.slack` (off by default) also posts a question to a Slack channel/user.
- Internal: consolidated the LLM/retrieval/runtime token + retry helpers.

## 0.14.1 — 2026-07-18

### Fix: scaffolded agencies get a unique memory collection

`fabri new agency` templates hardcoded their memory `collection` / `sqlite_path`
(e.g. `bug_triage_crew_parent`), so two agencies scaffolded from the same template
shared one memory store and cross-contaminated. Templates now use an
`__AGENCY_SLUG__` placeholder that the scaffolder fills from the agency name — e.g.
`fabri new agency alpha` → `alpha_parent` / `.fabri/alpha.db`.

## 0.14.0 — 2026-07-18

### Agency distribution: `fabri new agency`, `fabri studio`, examples in the wheel

Turn `pip install fabri` into a from-zero path to running and *watching* a multi-agent
agency — driven entirely by the CLI, so any AI coding tool (Claude Code, Codex, Grok
Build, Hermes) can use it via thin wrappers rather than bespoke integrations.

- **`fabri new agency <name> --template bug-crew|changelog|blank`** scaffolds a working
  agency (parent + specialist configs + a sandboxed workspace fixture + README) with
  paths correct for how fabri resolves them. No Claude Code required.
- **`fabri studio`** serves the bundled Studio UI and the run API from one same-origin
  server (no Vite proxy) — `pip install "fabri[sqlite]" && fabri studio` opens the
  Company view. Static serving is opt-in, so `fabri serve` is unchanged; a missing-assets
  build gives an actionable message.
- **Example agencies ship in the wheel** (`fabri examples` / `fabri examples --copy <dir>`),
  including dotfiles like `workspace/.gitignore`, with no stray bytecode.
- **Portable skill layer**: `skills/agency-builder/` is the one canonical, path-relative
  source, with thin per-platform briefs (`briefs/{codex,grok,hermes}.md`) that all drive
  the CLI.
- CI/release build the Studio assets, sync the bundled payloads, and assert the wheel's
  contents (Studio index+assets, example dotfile, no `.pyc`) before publishing.

## 0.13.2 — 2026-07-18

### Fabri Studio Company view: line icons + richer info-flow, and a second example agency

- **Cohesive line-icon set** replaces the emoji glyphs in the Company view — a
  monochrome, theme-aware set (lucide, inline SVG, `currentColor`) that stays on
  Studio's grayscale-survivable design system: Manager (briefcase), triage
  (search), fix (wrench), test (flask), research, write, verify, plan, deploy,
  security, data, code, plus document + coins + status badges.
- **Information-flow animations** now read for *every* agency, not just dynamic
  fan-outs: each agent plays a one-shot document-delivery + pop when it first
  enters the graph (static `tools.agents[]` specialists otherwise appeared with
  no flow), plus a status stamp-in and an active-connector highlight — all behind
  `prefers-reduced-motion`.
- **New example: `examples/agencies/bug-triage-crew`** — a triage → fix → test
  crew that localizes a real bug, edits code with `edit_file`, and runs the test
  suite, sandboxed to its `workspace/`. A second walking skeleton alongside
  changelog-release-notes and a good Company-view demo.
- Studio graph gains unit tests (`vitest`, wired into CI) pinning the trace→graph
  contract, including the nested tool-result envelope.

## 0.13.1 — 2026-07-18

### Fabri Studio: "Company" agency-graph view (example)

Studio's timeline showed a run linearly but never *which agent delegated to
which*. The new **Company** tab renders a run as a little company — the manager
and its specialist sub-agents as emoji-avatar characters, the tasks they hand
off, and a live payroll (COGS) counter — in two toggleable skins over one data
layer: a playful **Office** view (a document animates along each handoff while
it runs; agents light up idle → running → done/error) and a precise **Org
chart** view (node cards with status pill, cost, and tokens; edges labeled by
the handed-over task). A `Timeline | Company` toggle is also wired into the
read-only run replay.

It is derived **entirely client-side from the trace events Studio already
streams** — no service or Python changes, no new runtime dependencies. An agent
invoked more than once (a verify → repair → verify loop) dedupes to one node
with an ×N badge summing its cost and tokens, while per-call edges keep each
attempt's individual pass/fail.

### Fix: read the nested tool-result envelope in the agency graph

The graph read a `tool_call`'s `result` one level too shallow. A tool's result
is the canonical envelope `{ok, result, error}` (`fabri.tools.result`), with the
sub-agent's own return — `session_id`, `outcome`, `usage` — nested under
`result`. Reading it flat made static `tools.agents[]` specialists disappear
from the graph on real traces and dynamic sub-agents show `$0`/wrong status.
Now split into envelope-vs-child, with 15 unit tests (`vitest`) pinning the
contract and wired into the `studio-example` CI job.

## 0.13.0 — 2026-07-15

### Fix: cached tokens no longer double-billed on OpenAI & Gemini

`pricing.cost_for` charges cache-read tokens at 0.10x the input rate *on top
of* `input_tokens`, relying on `input_tokens` excluding cached tokens. That
holds for Anthropic and Bedrock but not OpenAI (`prompt_tokens` includes
`prompt_tokens_details.cached_tokens`) or Gemini (`prompt_token_count` includes
`cached_content_token_count`): both backends set `input_tokens` to the
cache-inclusive count *and* set `cache_read_input_tokens`, so cached tokens
billed at ~1.10x instead of 0.10x — an ~11x over-charge on the cached portion
(~3x on a warm 80%-cached turn), charged to real user credits. Both backends
now subtract cached tokens out of `input_tokens`; the invariant is documented
in `pricing.py`. Anthropic and Bedrock are unchanged. Two tests that had
encoded the bug are corrected, with a hand-computed money proof added.

### `fabri serve`: run cancel, persisted history, and fleet fan-out

The embeddable service gained thin seams over its existing per-run subprocess +
trace model (stdlib-only, no new engine): `POST /runs/<id>/cancel` terminates a
running agent; `GET /runs` lists run history, rebuilt from a new append-only
`index.jsonl` so it survives a `serve` restart; and `POST /fleets` fans one
batch out to N runs sharing a `fleet_id`, with `GET /fleets` / `GET
/fleets/<id>` rolling up member statuses and summed COGS. `submit()` gained
`thread_id` / `fleet_id` / `label` grouping tags.

### Cost surface: per-model COGS + run metrics on the result envelope

`extract_cost` now surfaces the `cost_by_model` breakdown and the token / step /
wall / sub-agent metrics the `usage` event already carried but the service
dropped, so a host (and Fabri Studio) can render a full COGS panel without
re-deriving anything.

### Fabri Studio: fleet-grade conversational + observability UI (example)

`examples/studio/` grew from a single-run demo into a three-surface front-end:
a live conversation with a plan timeline, real tool-call cards (args / result /
status / duration), a COGS panel, multi-turn threads, cancel/retry, run
history, and a Fleet view that rolls up N pipelines with their summed COGS and
per-item drill-down. Live-verified against a real OpenAI run.

### agency-builder: COGS is a first-class deliverable

The `agency-builder` skill now requires a Metrics & COGS section in the frame
(a concrete `agent.max_cost_usd` ceiling), makes quoting real `fabri report`
COGS part of the delivery gate, and wires Fabri Studio as the front-end a new
agency ships with (fleet mode for fan-out) instead of a bespoke dashboard.

## 0.12.1 — 2026-07-14

### Fix: outcome no longer reports success when every tool call failed

Two outcome-classification defects let the CLI's `outcome`/`success` field
report success on runs that did nothing. `_classify_outcome` treated
"the LLM emitted non-empty final text" plus "a tool failed" as an
unconditional `success_with_recovery`, so a run where 100% of dispatched
tool calls failed and no deliverable was produced could only ever report a
`success*` outcome. A new `all_tools_failed` outcome now fires when the run
succeeded-in-text but every dispatched call (≥2) failed; the ≥2 floor
preserves the legitimate single-early-failure-then-recovery case.
Separately, a nested `spawn_subagent` call that exited 0 while its own
outcome was `success_with_recovery` collapsed into a clean parent signal;
the parent now marks a tool failure whenever a nested child's outcome is
not exactly `success`, so a parent can never report a cleaner outcome than
its worst nested specialist. `agency-builder`'s SKILL.md and
`docs/agency-kernel.md` now make "run the deterministic verifier yourself
and quote its raw output; never trust `outcome` or the model's own
narration" a mandatory delivery gate.

### GPT-5 family support on the OpenAI backend

fabri can now drive OpenAI's GPT-5 / o-series models. The backend picked
the legacy `max_tokens` param, which those models reject with a 400; it now
selects `max_completion_tokens` for direct-OpenAI GPT-5/o-series while
keeping `max_tokens` for classic models and OpenRouter passthrough. GPT-5
reasoning models also reject function tools on `/v1/chat/completions` unless
`reasoning_effort` is `none`, so that is now sent for GPT-5 + tools.
Verified with a live end-to-end run of the changelog-release-notes agency
on `gpt-5.6-terra`, deliverable independently verifier-checked.

### Pricing via litellm; cost-effective OpenAI role defaults

Model rates now resolve dynamically from litellm's cost map (primary), with
the hardcoded table as a fallback that includes the GPT-5 family. The
litellm import's `load_dotenv()` side effect — which silently pulled the
working directory's `.env` into the environment and could change key
resolution — is now neutralized. The OpenAI narrator's cheap-model default
moves from `gpt-4o-mini` to `gpt-5-nano`; the example agency ships an
OpenAI variant set (manager on `gpt-5.6-terra`, specialists on
`gpt-5.6-luna`).

### `spawn_subagent`: per-spawn model override

The `spawn_subagent` tool accepts an optional `model` arg that overrides the
sub-agent's configured `llm.model` for a single spawn — e.g. pin a cheaper
execution-tier model for a domain child while the orchestrator keeps its own.

## 0.12.0 — 2026-07-14

### License change: Business Source License 1.1 → Apache License, Version 2.0

fabri returns to Apache-2.0, effective immediately and retroactively for
all previously BSL-licensed versions (0.6.0–0.11.0). No revenue
threshold, no commercial license, no embedding/hosting restriction.
`COMMERCIAL.md` and the `licenses-issued/` template are removed — no
commercial licenses were ever issued under the BSL terms, so this is a
clean revert.

### Governance change: the core is now open to contribution

Previously, PRs touching the agent loop, memory pipeline, orchestration,
or config surface were closed unmerged on principle. That's reversed:
core PRs are now welcome, gated on a design-issue-first discussion
rather than a blanket close. See CONTRIBUTING.md's new "Contributing to
the core" section for the process. Tools remain the fast,
no-discussion-needed path for new capability that doesn't need to touch
core.

### `skills/agency-builder` — scaffold a multi-agent agency from one prompt

A public, self-contained skill: give it one bounded deliverable and it builds
a small fabri agency (an orchestrator plus fixed specialists, each an ordinary
`tools.agents[]` entry) with a deterministic verifier standing in for "trust
me, it worked." Installable directly from this repo for both Claude Code
(`.claude-plugin/marketplace.json`) and Codex CLI (`.agents/plugins/marketplace.json`) —
`/plugin install agency-builder@fabri-skills` and
`codex plugin add agency-builder@fabri-skills` respectively. Both installs
were run for real (`claude plugin validate .`, a fresh
`codex plugin marketplace add` + `codex plugin add` showing the skill content
actually cached under the plugin root), not just schema-checked; an initial
Codex manifest bug (`skills` pointing at a nonexistent directory, caught by
review) is fixed.

Ships with `docs/agency-kernel.md` (what's fixed vs. per-agency) and a worked
example (`examples/agencies/changelog-release-notes`) whose committed
deliverable passes both the specialist verifier and the host repair check —
that's direct tool-level verification, not evidence of a completed live
multi-agent run, which needs a real provider key this environment didn't have.
The example's own README and the kernel doc both now carry an "Observe a run"
/ "After a live run" section pointing at `fabri traces show <session_id>` and
`fabri report`, with the caveat that static specialist cost doesn't currently
roll into the parent's reported total.

## 0.11.0 — 2026-07-12

### Wire the OTel trace export into the CLI (X1)

v0.10.2 landed the OTel exporter module but left it unwired. It's now wired:

- **`fabri traces export <session_id>`** — export a finished trace to the
  configured OTLP backend on demand, surfacing errors loudly (a missing
  `fabri[otel]` extra, an unreachable endpoint, or an unknown session all exit
  non-zero).
- **End-of-run auto-export** — `fabri run` exports the finished trace when
  `observability.otlp_endpoint` is set (`_maybe_export_trace`), best-effort: a
  broken or missing exporter logs a warning and never fails the run. It fires
  after the memory-compression usage event, so the spans include that cost too.

One top-level export nests spawned sub-agent traces underneath, so a single fire
captures the whole tree. Verified end-to-end against a local OTLP collector
(759-byte protobuf POST). Full recipes in `docs/observability.md`. Still pending
(future): a live inline span tap (B9) and threading the export through
`run_agent` for library callers / sub-agents without the CLI.

## 0.10.2 — 2026-07-12

### Runnable examples, a memory-pattern study, and an OTel export module

Three things ship together, all additive — no behavior changes to `fabri run`.

**A runnable `examples/` suite** (sqlite + gemini defaults, run from repo root),
each folder annotated with the optimization methodology it demonstrates:
`01-custom-tool` (tool contract + low-token design), `02-parallel-fanout`
(dynamic `spawn_subagent` with `parallel_group` + per-child budgets),
`03-pipeline-verifier` (static `tools.agents[]` draft→verify loop), and
`04-docker-sandbox` (layered `LocalSandbox`→`DockerSandbox` isolation). The
README's previously-illustrative multi-agent shapes now have working counterparts.

**New docs.** `docs/design/external-memory-patterns.md` surveys how Hermes Agent
and OpenClaw handle memory and proposes four adoptions (a `MemoryStore` Protocol,
an offline consolidation pass, an always-injected core digest, and finishing the
OTel wiring). `docs/using-fabri-well.md` documents the cross-run learning loop and
memory hygiene. `docs/optimization-methodologies.md` maps the transferable ideas
to real fabri mechanisms and the examples that show them.

**OTel export module (X1, off by default).** Adds `observability/otel.py` — a
post-hoc batch exporter that maps fabri's JSONL trace spine to an OpenTelemetry
span tree — plus the `observability:` config block, the `FABRI_OTLP_*` env
overrides (`_apply_env_overrides`), the optional `fabri[otel]` install extra, and
`docs/observability.md` (Langfuse + generic-OTLP recipes). The JSONL spine stays
the source of truth; OTLP is just an export target (Langfuse, Honeycomb, Datadog,
Tempo, Jaeger). Tool spans are keyed by `call_index`, so parallel_group fan-outs
that run the same tool concurrently map to the correct spans. **Not yet wired
into the CLI/run loop** — the `fabri traces export` verb and end-of-run fire
(B2/B3 in `docs/design/memory-observability-plan.md`) are still pending; this
release lands the exporter, config, and library entrypoint only.

## 0.10.1 — 2026-07-11

### Stream Anthropic responses so large turns don't truncate

The Anthropic backend used non-streaming requests, so its max_tokens retry was
held to a non-streaming-safe 16000 ceiling — a content-heavy turn (>16k output,
e.g. writing several files) truncated even after the retry and failed the run.
The backend now streams via `messages.stream()` + `get_final_message()` (same
Message shape downstream), and the streaming path gets its own
`ANTHROPIC_MAX_TOKENS_CEILING = 64000`. The openai/gemini backends keep the
16000 non-streaming cap.

## 0.10.0 — 2026-07-07

### Measured retrieval — observability, an offline eval gate, and a hybrid default that actually wins

Memory retrieval was *unmeasured* — every quality tweak was a guess. This
release makes it **measured, gated, observable, and better by default.** Full
plan: `docs/design/memory-observability-plan.md`; first-user tuning guide:
`docs/retrieval-tuning.md`.

- **Offline retrieval eval + CI gate (M4).** `python -m
  fabri.benchmarks.retrieval_eval` drives fabri's real `_retrieve_inner` over a
  labeled fixture (40 guidelines / 24 queries) and reports recall@k / MRR /
  precision@k per strategy — fast, deterministic, **zero API credits**. A pytest
  gate (`tests/test_retrieval_eval_gate.py`) locks the shipped defaults at
  `measured − 0.05` so retrieval can't silently regress. CI installs
  `.[dev,sqlite]`.
- **BM25 was a silent no-op on the SQLite backend — found by the eval, fixed.**
  `_fts5_query` space-joined tokens and FTS5 reads a space as implicit AND, so
  multi-word queries matched nothing and `sparse`/`hybrid` collapsed to `dense`.
  Fix: OR-join terms + split tool-name underscores. Guarded by
  `test_hybrid_bm25_is_alive`.
- **Default retrieval strategy flipped `dense` → `hybrid` (M5/D3).** Eval-backed,
  and hybrid degrades gracefully to dense wherever BM25 is unavailable, so it is
  never worse than the old default.
- **Two eval-driven retrieval-quality fixes** turned hybrid from "wins only at
  recall@5" into the best strategy on **every** metric: **RRF `k` 60 → 20** (new
  `memory.rrf_k`; recall@3 0.60 → 0.90 — the web-scale constant flattened rank
  over fabri's short two-pool fusion) and **`success_pattern` guaranteed slots
  back-loaded** (recall@1 0.13 → 0.58, MRR 0.45 → 0.84 — relevance now owns
  rank 1 and the guarantee fills reserved *tail* slots).
- **Retrieval-decision observability (M3).** One structured `retrieval` trace
  event per call — strategy, dense/sparse pool sizes, whether BM25 fired or
  silently fell back, guaranteed-slot counts, MMR, and a per-candidate list with
  `inclusion_reason`. Trace-only (zero prompt-token cost), emitted only when a
  `session_id` is threaded in. Makes "why is retrieval weak" debuggable.
- **Memory-health section in `fabri report` (M6).** Guidelines-in-store,
  strategic share, median entry age, and a per-kind breakdown across md/json/
  html. Offline-safe: opens the store best-effort so an unreachable backend
  can't kill the trace-only report.
- **Tuning knob:** `memory.rrf_k` (default 20). All other retrieval knobs
  unchanged.

### Fixed

- **Trace-ordering:** the run's root `start` event now emits *before* retrieval,
  so M3's `retrieval` event nests as a child rather than landing ahead of the
  root (fixes the `start`-is-first invariant relied on by reports and the
  upcoming OTel exporter).
- **Test isolation:** per-test Qdrant collection isolation kills the
  order-dependent accumulating-count flake seen in the July CI reds; the
  concurrent-ingest test warms the collection before racing so it exercises only
  the lost-update path it asserts.

## 0.9.2 — 2026-07-05

### The Improver — plug-and-play log ingestion (`fabri.readlogs`)

The self-improving memory loop could only ever learn from fabri's *own* runs —
`process_trace` was hard-wired to read a native-schema trace off disk. The
**Improver** opens that loop to any log you already have: point fabri at app
logs, CI output, or OTel/OpenAI traces and the failures + successes in them mine
into the *same* memory the agent retrieves from.

- **SDK-first.** `fabri.readlogs(source, *, adapter="auto", synthesize=False,
  store=None, config=None, ...)` returns an `IngestSummary` (sessions, events,
  `by_kind`, `skipped_lines`, `.entries`, and honest `llm_cost_usd`). The
  reusable object form is `fabri.Improver` / `Improver.from_config(...)` with
  `.ingest(...)` (batch: file, dir, stdin, or iterator) and `.ingest_stream(...)`
  (one summary per session as it flushes). Omit `store`/`config` and it resolves
  them from `agent.yaml`, so an ingested guideline lands in the collection the
  agent already reads.
- **Deterministic-first ($0).** The default path is LLM-free — it mines
  postmortems, deterministic failure/success text keyed on
  `(task, tool, error-signature)` (so repeats dedup/merge like postmortems), and
  never calls a provider (a `NoOpLLM` sentinel enforces it). `synthesize=True`
  (or `--synthesize`) turns on LLM guideline compression at token cost.
- **Adapters plug in three ways** behind one open registry: the
  `@fabri.adapter("name")` decorator, a declarative config field-map
  (`ConfigMapAdapter`, zero code), and a polyglot executable via the tool
  contract (`ToolAdapter`). Built-ins ship for `jsonl` (native passthrough),
  `regex` (plaintext), and `otel`/`openai` (structured traces), with
  `adapter="auto"` sniffing. Third-party adapters are discovered via the
  `fabri.adapters` setuptools entry-point group (log-and-skip on a bad plugin;
  `ingest.load_plugins: false` disables discovery).
- **CLI.** `fabri ingest SOURCE [--adapter NAME] [--synthesize] [--option K=V]
  [--dry-run] [--json]` (`-` = stdin), plus `fabri ingest --list-adapters`.
- **Config.** New `ingest` section (`default_adapter`, `synthesize`,
  `load_plugins`, `record_postmortems`, `adapters: []`). Declared `configmap`/
  `tool` adapters are addressable by name with no Python.
- **Enabling refactor.** `process_trace` gains backward-compatible `events=` and
  `synthesize=` params — pass an in-memory event list instead of reading a trace
  file, and swap the two LLM miners for deterministic text. Every existing caller
  is byte-identical (full suite green with Qdrant).
- **Example skill** `syslog-adapter` demonstrates the polyglot `ToolAdapter`
  path end to end (`fabri skills install` → `fabri ingest app.log --adapter
  syslog`). New package `src/fabri/ingest/`; 22 offline tests.

### Fixes

- **Ingest `peek()` no longer double-counts in-memory logs.** `LogSource.peek()`
  re-appended the whole original iterable after a fresh iterator, so a list/tuple
  source (the default `fabri.readlogs([...])` auto-sniff path) yielded every item
  twice — doubling mined signals and skewing promotion thresholds. Generator/file/
  stdin paths were unaffected. Set the raw source to the remainder iterator instead.
- **`configmap` string status.** A mapped `ok_field` string like `"false"`/`"error"`
  was truthy under `bool()` and mis-mined a failed call as success; routed through
  the shared fail-word check.
- **Restored a green suite (CI had been red since v0.9.0).** Repaired three
  pre-existing test defects the release build inherited: the eviction unit stub
  deleted by a synthetic id instead of `entry.id`; the domain classifier's `code`
  keyword was `"file_"` (never matched `read_file`); and a stale
  `agent_runner_tool.QdrantMemoryStore` monkeypatch (the store is built via
  `build_memory_store` since the v0.9.0 runtime refactor). Full suite green (871
  passed) with a live Qdrant.

## 0.9.1 — 2026-07-04

### Docs patch

- `TODO.md`: v0.9.0 retrieval items marked done; open follow-ups tracked (query expansion, reranking, agent namespacing, TTL/eviction).
- `docs/ROADMAP.md`: M2 card added (hybrid retrieval, shipped v0.9.0); Track M description and mermaid diagram updated; suggested build order updated.

## 0.9.0 — 2026-07-04

### Hybrid & Advanced Retrieval — BM25+Vector fusion, temporal decay, MMR, domain routing

**Memory schema enrichment**

- `MemoryEntry` gains four new optional fields: `domain` (`code`/`planning`/`search`/`api`/`generic`), `outcome` (`failure`/`partial`/`success`/`unknown`), `agent_id`, and `task_embedding_hash`. Fully backward-compatible: old DB payloads return safe defaults via `.get()`. Deterministic ID hash is unchanged — no migration needed.
- `ingest_guideline` auto-classifies `domain` and `outcome` on every new entry at write time. Merges preserve the original classification.

**Retrieval strategies** — controlled by `memory.retrieval_strategy`

- `"dense"` (default) — original cosine vector similarity, behavior unchanged.
- `"sparse"` — BM25-only via SQLite FTS5 (Python built-in, zero extra install) or client-side `rank_bm25` for Qdrant.
- `"hybrid"` — **Reciprocal Rank Fusion** (RRF, k=60) of dense + sparse. For SQLite, FTS5 gives a true independent BM25 index over the full table; for Qdrant, `rank_bm25` re-ranks the dense pool (install with `pip install 'fabri[bm25]'`).
- `"hybrid+mmr"` — hybrid + **Maximal Marginal Relevance** diversification on the final candidate pool. Iteratively selects entries balancing relevance vs redundancy (lambda tunable via `memory.mmr_lambda`).

**Scoring pipeline** (all opt-in, safe defaults)

- `memory.temporal_decay: true` — exponential decay by entry age: `score *= exp(-ln(2) * age_days / half_life_days)`. Recent entries ~1.0, `half_life_days`-old entries ~0.5. Configurable via `memory.temporal_half_life_days` (default 30).
- `memory.importance_weight: 0.2` — dynamic importance boost from `hit_count` + strategic promotion bonus. `importance = min(1, hit_count/10 + 0.3 if strategic)`. Applied as `score *= (1 + weight * importance)`.
- `memory.domain_routing: true` — keyword heuristic classifies query domain (no LLM call, zero latency); matching entries get a 1.15× boost. Never hard-filters — a mismatch applies no penalty.

**SQLite FTS5 index**

- New `fts_guidelines` FTS5 virtual table (porter tokenizer) synced on every upsert/delete.
- One-time migration: existing DBs are bulk-populated on first upgrade.
- `_fts5_query()` sanitizes input (splits on non-word chars, wraps tokens in double quotes, caps at 50 tokens) so tool names with underscores, URLs, and special chars never cause a syntax error.

**Config keys added** (all default to pre-v0.9.0 behavior)

```yaml
memory:
  retrieval_strategy: dense        # dense | sparse | hybrid | hybrid+mmr
  temporal_decay: false
  temporal_half_life_days: 30.0
  mmr_lambda: 0.7                  # 0=pure diversity, 1=pure relevance
  domain_routing: false
  importance_weight: 0.2
  query_expansion: false           # reserved
```

**Optional dependency**

- `pip install 'fabri[bm25]'` installs `rank-bm25` for Qdrant hybrid retrieval.

**Bug fix**

- `agent_runner_tool.py` previously hardcoded `QdrantMemoryStore`; now uses `build_memory_store(mem_cfg)` so SQLite users get hybrid retrieval in sub-agent runs too.

**Tests**

- `tests/test_unit_hybrid_retrieval.py` — 30 pure-Python tests for `RetrievalConfig`, RRF, temporal decay, importance scoring, domain classifier, and MMR. No external deps.
- `tests/test_unit_sqlite_fts5.py` — FTS5 schema, migration, BM25 query, sync-on-delete, and special-char safety tests (skipped without sqlite-vec).
- `tests/test_memory_store.py` — backward-compat round-trip for old payloads; new field round-trip; ID stability proof.

## 0.8.2 — 2026-07-01

### AWS Bedrock provider (Converse API) + Provider enum

- **`llm.provider: bedrock`** — a fifth provider, on the Bedrock **Converse API**
  via boto3. One backend serves every Converse-capable model (Claude, OpenAI
  `gpt-oss`, Llama, Mistral, Moonshot Kimi, …). Credentials resolve via the
  standard AWS chain (env keys / shared profile / IAM role /
  `AWS_BEARER_TOKEN_BEDROCK`); region from **`llm.aws_region`** or `AWS_REGION` /
  `AWS_DEFAULT_REGION`. No `api_key_env` — a `bedrock` role forces it to `None`
  (even if `DEFAULT_CONFIG`'s gemini key leaks in via deep-merge), and a
  boto3-free region pre-flight replaces the api-key pre-flight so a missing
  region fails early with a clean message.
- **`BedrockLLMBackend`** translates fabri's Anthropic-shaped history to/from
  Converse `toolUse`/`toolResult` blocks, coalesces consecutive same-role turns
  (Converse requires strict alternation), threads `maxTokens` through
  `inferenceConfig`, parses response blocks by key presence, keeps the
  max-tokens retry-once parity, and maps terminal stopReasons (incl.
  `model_context_window_exceeded`) to `LLMError`. boto3/botocore import lazily,
  so `import fabri.*` stays dependency-free.
- **`Provider` (StrEnum)** in `core/llm.py` is now the single source of truth for
  provider ids — dispatch, the default-api-key map, config normalization,
  dry-run, and the ideator reference it. Adding a provider = one enum member + a
  dispatch branch.
- **Pricing** entries for Bedrock, incl. `us.`-prefixed inference profiles and
  Moonshot Kimi K2.5 / K2-thinking.
- New dep: **`boto3>=1.39`** (floor for automatic `AWS_BEARER_TOKEN_BEDROCK`
  detection). Example configs `configs/bedrock.yaml` (Kimi K2-thinking
  orchestrator) + `configs/bedrock_subagent.yaml` (Kimi K2.5 worker).

## 0.8.1 — 2026-06-29

### Bounded parallel sub-agent fan-out

- **`tools.max_parallel_spawns`** (default `4`) caps how many `spawn_subagent`
  calls in one `parallel_group` run concurrently. Each spawn is a fresh
  subprocess, so an unbounded fan-out (`max_workers=len(group)`) let a wide wave
  spike memory without limit — enough to OOM-kill the host process in a
  memory-capped container. Members beyond the cap now queue and run as slots
  free up: identical group result, bounded peak concurrency (and memory).
  Threaded through `AgentRunConfig` so sub-agents inherit the parent's cap.
  Lower it on tight memory budgets.

## 0.8.0 — 2026-06-28

### Track B — the builder layer (idea → running self-improving agent)

A new `src/fabri/builder/` package and a `src/fabri/service/` package turn the
engine into a product factory: scaffold agents, tools, and prompts from intent,
package reusable bundles as skills, and embed the whole thing as a self-contained
service. See `docs/vision.md` for the engine+builder thesis.

- **Ideator** (`fabri ideate "<idea>"`) — a one-line product idea becomes a
  *reviewable* scaffold dir (agent.yaml + prompts + tool stubs) via fabri's own
  structured output. Emits for review; never auto-applies.
- **Tool-writer** (`fabri tool new|validate|test`) — a description or a Python
  function signature becomes a tightened-schema manifest (not opaque `{}`) + a
  calling stub + a local test. `fabri tool validate` / `fabri tool test` close
  the no-validation / no-local-test gap.
- **Prompt-kit** — a nine-section prompt skeleton (`fabri prompt new`) plus a
  user-prose / `<!-- AGENT_MEMORY -->` output split wired (additively) into the
  trace miner.
- **Wave planner** — `builder.waves.plan_waves` topologically layers declared
  dependency edges and auto-assigns `parallel_group` for fan-out.
- **Discovery / runner ergonomics** — `fabri tools [--search]`, `fabri tool run`,
  and `fabri agent run --dry-run` (resolves config + tool defs with no network).
- **Skills registry** (`fabri skills add|list|install`) — installable bundles of
  prompt + tool manifests + a config snippet, with a bundled example skill.
- **Self-contained service** (`fabri serve`) — binds a per-run config from one
  template + overrides, spawns the agent, streams events by tailing the JSONL
  trace over stdio + HTTP/SSE, and surfaces cost. A non-Python host can drive a
  run with no fabri imports. Streams via the trace, so it needs neither O2 nor
  any change to the agent loop.
- **Repair loop** (`agent.repair`, **off by default**) — a bounded
  verify → repair → rerun loop that injects the verifier output as context and
  stops on no-progress (same error signature twice) or at `max_attempts`, with a
  fresh step budget per attempt. Threaded through `AgentRunConfig` so it
  activates from config at run / replay / agent-runner.

All builder code is additive, stdlib-only (no new third-party deps), and
project-agnostic; 116 new offline tests. Builds on v0.7.9 (Gemini).

## 0.7.9 — 2026-06-28

### Google Gemini support + Gemini is now the default provider

- **New `gemini` provider** on Google's native `google-genai` SDK
  (`core.llm.GeminiLLMBackend`), implementing the full `LLMBackend` contract.
  Translates fabri's Anthropic-shaped history into Gemini `Content`/`Part`
  schema and back, routes the system prompt to `system_instruction`, sanitizes
  tool JSON-schema for `FunctionDeclaration`, and matches a `function_response`
  to its call by NAME (fabri synthesizes the tool-call id Gemini omits and
  rebuilds an id→name map each turn). MAX_TOKENS retry, token-folding, and
  transient-error handling at parity with the Anthropic/OpenAI backends.
- **Gemini is the default provider.** `DEFAULT_CONFIG` now defaults to
  `provider: gemini`, `model: gemini-2.5-pro`, narrator `gemini-2.5-flash-lite`
  (lowest cost + generous free tier). Existing Claude/OpenAI configs keep
  working unchanged; only the no-provider default moved.
- **All provider SDKs ship by default.** `google-genai` + `openai` are now core
  dependencies alongside `anthropic`, so any provider works with no extra
  install; the `openai`/`gemini` extras are removed. `benchmark.yaml` stays on
  `claude-sonnet-4-6` to keep published BENCHMARKS reproducible.
- **Pricing** entries added for `gemini-2.5-pro` / `2.5-flash` / `2.5-flash-lite`
  / `2.0-flash`, so Gemini runs are priced into COGS like every other model.
- **`scripts/smoke_gemini.py`** — live end-to-end smoke test that forces a tool
  call and proves the round-trip via a per-run random token (with a `--mock`
  harness self-check that needs no API key).

## 0.7.8 — 2026-06-25

### COGS accuracy fixes

- **Planner LLM usage is now accumulated.** `core.planner.plan()` accepts an
  `on_usage` callback; `run_agent` passes its `_accumulate` so a planner
  step's tokens (often a full-context Sonnet pass) land in the run's
  reported `cost_usd` / `total_cost_usd` instead of silently leaking.
- **Decompose LLM usage is now accumulated.** `core.decompose.decompose()`
  accepts `on_usage`; threaded through `_dispatch_tool_calls` so every
  `decompose` tool call rolls into COGS.
- **Memory-compression LLM usage is captured.** `synthesize_guideline` and
  `synthesize_success_pattern` accept `on_usage`; `orchestrator.pipeline.
  process_trace` threads it through. `cli.cmd_run` accumulates and emits a
  new `post_run_usage` trace event after `process_trace` completes so a
  host can merge the cost onto the run's recorded totals.
- **`cost_unaccounted` event for crashed sub-agents.** When `spawn_subagent`
  fails without surfacing usage (e.g. qdrant down → runner crashed before
  printing its final JSON), the parent now emits an explicit
  `cost_unaccounted` event with `tool`, `step`, `reason`,
  `child_returncode`, and `child_stderr_tail` so a host can warn that
  recorded COGS is a lower bound for the run rather than silently
  under-reporting.
- New `EventType.COST_UNACCOUNTED` and `EventType.POST_RUN_USAGE`.

### Orchestration internals & CLI consolidation

- **`AgentRunConfig`** (`fabri.core.run_config`) — a single value object for
  the ~18 scalar orchestration knobs `run_agent` consumes, built once from a
  loaded config via `from_config` and threaded into every entry point. Fixes a
  real divergence bug: `fabri replay` and the agent-as-tool runner previously
  re-listed the kwargs by hand and **silently dropped** the planner,
  tool-retrieval, and budget settings — so a replay ran under different
  orchestration than the original (defeating the point of replay), and a
  sub-agent never used the planner/retrieval even when configured. All three
  entry points now share `runtime.build_run_llms` + `AgentRunConfig`.
- **`llm.planner` role now actually builds the planner backend.** `cmd_run`
  wired `planner_llm=build_decompose_llm(...)`, so the dedicated `llm.planner`
  config was dead. Added `runtime.build_planner_llm`; the planner role is now
  honored (falls back to decompose, then main, exactly as before when unset —
  no default-behavior change).
- **One step engine.** The planner executor and the non-planner single loop
  were copy-pasted and had already drifted (only the single loop emitted
  per-step `cost_usd`). Unified into `_run_step_loop`; the planner path now
  gets the same per-step cost telemetry, and dispatch/error/budget/nudge logic
  can't diverge between paths again.
- **Trace renderer extracted** to `fabri.orchestrator.trace_render` (pure,
  unit-tested) out of `cli.py` — `fabri traces show`/`tail` are unchanged.

### Postmortem memory (ROADMAP card **M1**, first increment) — opt-in

- **`memory.record_postmortems`** (default `false`) — when set, every run
  (any outcome) writes one deterministic, LLM-free whole-run postmortem to
  memory: `task + outcome + steps + tool-call/failure counts + repeated
  (tool × error-signature)` groups. It's a new `postmortem` memory kind with
  its own point-id namespace and same-kind dedup, retrieved by task similarity
  so a similar future task surfaces "last time this took N steps; tool X failed
  K times". Off by default, so entry counts/contents are unchanged for callers
  that don't opt in. The harder `final_diff`/`fix_pattern` extraction remains a
  follow-up (still tracked in TODO P2).

### Structured / typed output (ROADMAP card **O1**)

Opt-in and backward compatible: configs without `agent.response_schema` are
unchanged and pay zero extra LLM calls.

### Added

- **`agent.response_schema`** — an optional JSON Schema. When set, the
  final answer is parsed as JSON and validated against it. On a mismatch
  the runner re-prompts the model with the human-readable validation
  errors up to **`agent.response_retries`** times (default `1`). The
  validated value is returned on the run result as **`structured_output`**
  (and surfaced by the sub-agent runner so a parent spawn can read a
  child's typed result); `final_text` still carries the raw string.
- **`agent.error_strategy`** — how an un-satisfiable schema resolves after
  retries: `strict` (default) ends the run with the new
  **`Outcome.INVALID_OUTPUT`**; `warn` returns the unvalidated text as
  success; `fallback` returns **`agent.response_fallback`** (or `{}`) as
  success.
- **`structured_output` trace event** — one per validation attempt,
  carrying `attempt`, `valid`, and the `errors`, so a trace shows how many
  retries a typed answer cost.
- **`fabri.core.structured`** — a small, dependency-free validator for the
  JSON-Schema subset that matters for LLM output (`type` incl. type lists,
  `properties`, `required`, `items`, `enum`, nested objects/arrays).
  Unknown keywords are ignored rather than erroring. Not a full Draft-2020
  implementation by design.

### Notes

- Validation lives at the agent-loop layer (`core/agent.py`), not in the
  provider backends — `core/llm.py` is untouched, so every provider gets
  structured output for free.
- Structured output applies to the single-loop (non-planner) final answer.
  When the planner engages, the schema is skipped with a logged warning
  (the planner concatenates per-item outputs, so a single schema doesn't
  apply).

### Security & robustness hardening

A focused audit pass (subprocess tools, sandbox, orchestration, memory, LLM/MCP)
fixed the following active issues:

- **Sub-agent recursion cap.** `spawn_subagent` now threads
  `FABRI_SUBAGENT_DEPTH` through the child env and refuses to spawn past
  `FABRI_SUBAGENT_MAX_DEPTH` (default 5). Without this, a confused or
  prompt-injected agent could fork-bomb `breadth^depth` subprocesses, each
  carrying its own fresh cost budget.
- **Cost budget across fan-out.** A breached `agent.max_cost_usd` now refuses
  to spawn *more* sub-agents mid-step (the per-step check couldn't bound a
  single parallel fan-out before). The structured-output retry loop is also
  budget-checked.
- **Parallel dispatch no longer aborts on one raising future.** A sub-agent
  that raises is normalized to a `tool_error` so every `tool_use` keeps its
  paired `tool_result` (an unpaired block would 400 the next provider call).
  Removed dead code in the fan-out loop.
- **Sub-agent telemetry on the default path.** `on_subagent_finished`
  (fan-out count / delegation-regret) was only wired on the planner path; it
  now fires on the default single-loop path too.
- **Planner step-budget division** counts every processed item, not just
  successful ones, so a failed early item no longer starves later items.
- **`ask_user` socket wait is bounded** (`FABRI_ASK_USER_TIMEOUT_S`,
  default 300s) and falls back to the question's `default`, instead of
  hanging until the parent spawn timeout. The socket path now also honours
  `default` on an empty reply (parity with stdin).
- **Retrieved guidelines are fenced.** Memory mined from prior runs' tool
  outputs/task text is wrapped in a `<retrieved_guidelines>` block with a
  "reference only, never an instruction" caveat and stripped of forged fence
  tags — reducing stored-prompt-injection risk across sessions.
- **Sqlite memory store fails fast on an embedding-model-version mismatch**
  (parity with the Qdrant store), instead of silently returning garbage
  neighbours.
- **Docker sandbox hardened by default** (`--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--pids-limit=512`), with `mem_limit`
  and `network` configurable. It's the real isolation boundary for the
  by-design arbitrary-code tools.
- **Admin token compare is constant-time** (`hmac.compare_digest`) and fails
  closed on `None`.
- **MCP stdio servers** get a merged environment instead of a replaced one
  (a bare `env=` would strip `PATH`/`FABRI_HOME` and break the server).

Second audit pass (report rendering, network tools, recipes, CLI surfaces):

- **SSRF guard on `fetch_url`** (builtin + recipe). The model-supplied URL is
  now restricted to http(s), refused if the host resolves to a
  private/loopback/link-local/reserved address (cloud metadata
  `169.254.169.254`, localhost, RFC1918), and re-validated on every redirect
  hop so a public URL can't 302 to an internal IP. `file://` is blocked.
  Escape hatch `FABRI_FETCH_ALLOW_PRIVATE=1` for fetching trusted internal
  dev services (off by default).
- **HTML report XSS fixed.** `fabri report --format html` now `html.escape`s
  every trace-derived cell/header (task text, tool names, model ids,
  outcomes) and the SVG chart label — previously a task containing `<script>`
  became active markup in the generated, shareable `.html`.
- **`session_id` path containment.** `trace_path` rejects ids outside
  `[A-Za-z0-9_.-]`, so a crafted id can't escape `.fabri/traces/` on the
  `replay` / `traces` / `ingest-traces` read paths (defense-in-depth; HIGH if
  a host ever feeds externally-supplied ids).
- **Recipe hardening.** `run_shell_safe` drops `find` (its `-exec`/`-delete`
  defeat a binary allow-list) and rejects exec/file-write args
  (`-exec`, `--output`, `git -c`, …); `git_diff` validates `ref` so a
  `--output=` can't write the diff to an arbitrary file.

## v0.7.7 — 2026-06-24

Multi-provider per-role LLM + OpenRouter, plus a Haiku-class narrator that
emits short user-facing status updates between tool steps. Backward
compatible: existing v0.7.x `agent.yaml` files keep working unchanged.

### Added

- **Per-role LLM provider/model selection.** `llm.decompose`, `llm.planner`,
  and `llm.narrator` accept either a model-id string (legacy shorthand) or
  a full dict `{provider, model, api_key_env, max_tokens, base_url,
  cache_messages}`. Each role bills against its own API key; the four
  roles can run on three different providers simultaneously. Inherits any
  missing field from the parent `llm.*` defaults.
- **New provider keyword: `openrouter`.** OpenAI-API-compatible; the
  backend pins `base_url=https://openrouter.ai/api/v1` automatically.
  Model ids are namespaced (e.g. `anthropic/claude-haiku-4-5`).
- **`OpenAILLMBackend(base_url=...)` kwarg.** Optional; lets the same
  backend talk to any OpenAI-compatible endpoint. Pure addition — old
  callers see no signature change.
- **Haiku narrator emits `narration` trace events between tool steps.**
  Configured via `llm.narrator` (defaults to `claude-haiku-4-5`,
  effectively free per run); set to `null` to silence. Failures are
  swallowed so narration never breaks a run; usage rolls into the run's
  `total_cost_usd`. New `run_agent(narrator_llm=...)` parameter.
- **`runtime.build_role_llm(config, role, tool_defs=None)`** — single
  resolver that powers `build_llm` / `build_decompose_llm` /
  `build_narrator_llm` (now one-line shims). Adding a new provider means
  one branch in `runtime._instantiate`.
- **`runtime.find_missing_role_api_keys(config)`** — walks every
  configured role and returns `{env_var: [roles]}` for the env vars that
  aren't set. CLI + benchmark pre-flight now reports ALL missing keys in
  one error instead of failing on the first.
- **Pricing entries for common OpenRouter model ids** —
  `anthropic/claude-{haiku-4-5,sonnet-4-6,opus-4-8}`,
  `openai/{gpt-4o,gpt-4o-mini}` — match the underlying provider's list
  price; reconciled to the OpenRouter invoice on adoption.

### Changed

- **`config._normalize_llm_roles`** runs inside `load_config` (and lazily
  inside `runtime._resolve_role_cfg` for callers that bypass
  `load_config`). Lifts legacy flat keys (`decompose_model`,
  `narrator_model`, `narrator_max_tokens`) into the new role shape with
  no warning. If both legacy and new exist for the same role, the new
  dict wins — clean incremental migration.
- **Memory store now fail-fasts on embedding-model mismatch.**
  `_ensure_collection` scrolls one existing point and raises with a
  clear "recreate-or-rename" message when its `model_version` differs
  from the running embedding model. Previously this would silently mix
  embedding spaces.
- **Tool manifest arg-rewriting tightened.** A token like `grep.py` in
  `bash -c "ls grep.py"` no longer gets rewritten to an absolute path
  just because a sibling file named `grep.py` exists. Only path-shaped
  tokens (script extension or containing `/`) qualify.

### Removed

- **Dead `evict_stale` in `memory/pruning.py`.** No callers, and the
  gate could never fire given how promotion grows `hit_count`.
- **Narrator provider-mismatch heuristic (`_NARRATOR_PROVIDER_DEFAULTS`,
  `_is_anthropic_model_id`, `_is_openai_model_id`)** in `runtime.py` —
  ~30 lines. The per-role `provider` keyword replaces it; the heuristic
  was guesswork the user can now state explicitly.

### Compatibility

- No DB / disk format change. No on-wire trace event change (new
  `narration` event is purely additive).
- `LLMBackend`, `run_agent`, `build_llm`, `build_decompose_llm`,
  `build_narrator_llm`, `AnthropicLLMBackend`, `OpenAILLMBackend` all
  preserve their existing signatures. `OpenAILLMBackend.__init__` gains
  one optional kwarg.
- A v0.7.6-shape `agent.yaml` produces the same backend selection it
  always did. A pin-test (`test_legacy_config_unchanged_backend_selection`)
  guards against drift in the lift logic.

### Tests

490 passed (469 without the optional `openai` extra). New:
`test_unit_role_resolution.py` (15 cases incl. legacy-config pin),
`test_unit_openrouter_backend.py` (3 cases), 3 OpenRouter pricing cases,
narrator dedup/empty-drop/multi-step/usage-rollup coverage in
`test_unit_narrator.py`.

## v0.7.6 — 2026-06-23

The "public source release" pass. No agent-loop semantics change; no
config-shape change; no memory schema migration. A host service that
uses fabri as a library needs zero changes.

### Added

- **`SqliteMemoryStore` re-exported from `fabri`** — `from fabri import
  SqliteMemoryStore` now works alongside `QdrantMemoryStore`, matching
  the in-process backend that `pip install 'fabri[sqlite]'` promotes.
  The library example in the README and `docs/creating-an-agent.md`
  picks it up; existing code keeps working unchanged.
- **`ToolHandler` type alias** in `tools/registry.py` for callable-backed
  tools; tightens the type hints around `register_callable()` (was
  previously `dict[str, "callable"]`, a stringified built-in).

### Changed — docs & READMEs

- **README licence + PyPI links rewritten as absolute GitHub URLs** so
  they resolve on the rendered PyPI page (previously relative paths
  404'd outside the repo). Added BUSL-1.1 / PyPI / Python-version
  badges.
- **MCP-servers documented in the config-schema section** of the README.
  The feature has shipped since v0.7.2; it was previously discoverable
  only by reading `config.py`.
- **TODO.md P3 backlog cleaned up** — the items marked open in v0.7.1's
  hardening pass (read_file/edit_file byte cap, decompose fence strip,
  admin gate warning, reserved decompose name, embed() empty/whitespace
  reject, OpenAI parallel-tool-call) are now correctly checked off, each
  with a `_(v0.7.1)_` annotation.

### Changed — code comments

- **Internal ticket-prefix comments (`# G9`, `# A1`, `# S2`, `# P3`,
  `# F2`, ...) stripped or rewritten as plain English** across
  `config.py`, `core/agent.py`, `cli.py`, `runtime.py`,
  `orchestrator/retrieval.py`, `tools/registry.py`,
  `tools/agent_runner_tool.py`, `tools/examples/read_file.py`,
  `memory/store.py`, `memory/compress.py`. Internal-tracker shorthand
  meant nothing to anyone outside the project. The few comments that
  carried genuine WHY (tool-filter invariants, A4 dedup semantics,
  tokenizer-approximation note, provider-quirk fallbacks) survive
  verbatim.
- **Narrative WHAT-blocks collapsed** in `core/agent.py` (system-prompt
  policy block, cost-rollup, sub-agent telemetry) and `config.py`
  (DEFAULT_CONFIG inline narration). Behaviour unchanged; the modules
  read in one screen instead of three.

### Tests

- Suite stays green at 449 passing. Comment-only edits and the
  `SqliteMemoryStore` re-export don't touch observable behaviour.

## v0.7.5 — 2026-06-23

The "host-integration ergonomics" release. Three host-integration pain
points surfaced from a long fan-out orchestrator run: the host did the
work, narrated nothing on the last step, and was reported as a failure.

### Added

- **Terminal `incomplete` / `failed` trace events now carry `text`** — the
  model's last assistant utterance (last non-empty `final_text` or
  `thinking_text` from the run). Hosts that surface a recap after a
  max-steps termination no longer have to scrape `thought` events
  heuristically. The `final` event keeps its existing `text` (the
  model's `final_text`) unchanged; `outcome` and `reason` are unchanged
  on the other two. Strictly additive.
- **Final-step nudge** — on the LAST allowed step, the agent loop appends
  a one-shot "this is your FINAL step; stop calling tools and answer
  now" instruction to the last user message. Converts the common
  "did-the-work-ran-out-of-narration-budget" case into a clean
  `success` with real `final_text` instead of an `incomplete`
  termination. Gated on `max_steps > 1` so single-step runs are not
  perturbed. Active in both the legacy and planner-item loops.
- **`agent.subagent.{max_steps, max_cost_usd}`** — independent budget for
  spawned sub-agents. A host that raises the orchestrator's `max_steps`
  to give a fan-out room no longer inflates every child's budget too.
  Each field falls back independently to the parent's value when unset
  (default `null` for both → identical pre-v0.7.5 behaviour).
  `agent_runner_tool.py` (the spawn entry point) now also forwards
  `max_cost_usd` to `run_agent`, which it didn't before.
- **Design note: `docs/design/repair-loop.md`** — proposed
  verify → repair → bounded-rerun loop primitive (Item 3 from the host
  integration triage). Not implemented in this release; the note maps
  the config shape, loop semantics, cost-budget composition, and the
  open questions to resolve before coding. Targeted for v0.8.

### Tests

- `tests/test_unit_v075_features.py` — seven new unit tests covering:
  the `text` field on terminal `incomplete` and `failed` events, the
  final-step nudge converting `incomplete` → `success` (and the gate
  that suppresses it at `max_steps=1`), and the three subagent-budget
  combinations (full override, no override, partial override).

### Notes

- No change to the `Outcome` enum values, the `final` event shape, or
  `fabri.cli`'s exit-code mapping. The `text` addition to `incomplete` /
  `failed` is the only on-wire change, and it's a new optional field —
  existing trace readers ignore it.

## v0.7.4 — 2026-06-23

### Fixed

- **`SqliteMemoryStore` was missing a `collection` attribute** that
  `memory/pruning.py` reads to derive its per-collection ingest-lock file
  name. As a result, any sqlite-backed agent run that produced a
  `success_pattern` (or any other guideline) crashed during post-run trace
  mining with `AttributeError: 'SqliteMemoryStore' object has no attribute
  'collection'`. Found by the first real `session_delta` benchmark run
  against `configs/benchmark.yaml`. The end-to-end fabri × sqlite path was
  green at the store-API layer (the dedicated tests for the backend pass)
  but the pipeline integration had never been exercised.
- `SqliteMemoryStore.__init__` now takes a `collection: str = "fabri"`
  argument and stores it on the instance.
- `runtime.build_memory_store` passes `mem_cfg.get("collection", "fabri")`
  through to it, matching what it already does for `QdrantMemoryStore`.

### Tests

- The fix is covered by the existing per-store test pass plus a smoke check
  from the failing benchmark run; a dedicated pipeline-integration test for
  the sqlite backend lands in v0.7.5 (it requires a scripted-LLM end-to-end
  fixture for trace mining and is a follow-up).

## v0.7.3 — 2026-06-23

The "benchmark methodology lockdown" release. Ships two canonical configs +
a methodology doc so every future benchmark number is reproducible against
a specific fabri version.

### Added

- **`configs/example.yaml`** — runnable starter config (sqlite-vec memory,
  Sonnet 4.6, minimal tool surface). Allowed to drift across releases;
  it's a teaching artifact, not a contract.
- **`configs/benchmark.yaml`** — the LOAD-BEARING config every published
  fabri benchmark runs against. Locked per minor version: any value
  change requires a minor version bump AND a results-table note in
  `BENCHMARKS.md`. Each field carries an inline comment explaining the
  strategic call.
- **`configs/README.md`** — short pointer at the two files + quickstart.
- **`BENCHMARKS.md`** at the repo root — methodology, reproduction
  commands for both `session_delta` and LongMemEval, and empty results
  tables ready to accept rows as real runs land. Cites the comparison
  numbers (Mastra 94.87% on LongMemEval, Letta, Mem0, Zep) inline so the
  comparison is honest when the first fabri row gets filled in.
- **README hero block** updated with the no-docker `pip install
  'fabri[sqlite]'` path + a pointer at `configs/` and `BENCHMARKS.md`.

### Why this lands before any real benchmark number

The number you publish is only worth what you'd let someone re-run it.
Locking `configs/benchmark.yaml` first means every future "fabri got X% on
Y" can be reproduced against a specific fabri version, not just "whatever
config the demo happened to use." This is the spine the benchmark platform
work hangs off of.

### Operational

- No code changes; pure docs + configs.
- `configs/*` is included via setuptools defaults (no `package-data`
  change needed — these live at the repo root, not in the wheel; they're
  cloned/forked from GitHub).
- Tests: suite stays at 442 (no new tests; configs are validated by
  `load_config()` round-tripping in `__main__`, see release notes).

## v0.7.2 — 2026-06-23

The "clear the deferred backlog" release. All eight deferred items from
v0.7.1's CHANGELOG land here. Two are opt-in (G9 budget, G21 caching) so
ludexel keeps current behaviour unless it sets the new config keys.

### Added — opt-in features (default off; zero impact unless configured)

- **G9 cost-budget enforcement.** New `agent.max_cost_usd` config knob. When
  set, the run breaks out cleanly with `Outcome.BUDGET_EXCEEDED` before
  issuing an LLM call whose result would push total COGS (own + sub-agent
  subtree) past the threshold. Default: `null` (no budget; current behavior).
  Emits a `budget_exceeded` trace event with the step + threshold for
  observability.
- **G21 extended prompt caching.** New `llm.cache_messages` config knob
  (Anthropic-only). When true, marks the LAST message's tail content block
  with `cache_control: ephemeral`, so the conversation history prefix reads
  from Anthropic's 5-min cache (~0.1× input bill on the cached prefix) on
  subsequent turns. Default: `false`. Mutates a shallow copy — caller's
  messages list is untouched. Anthropic's 4-breakpoint limit is respected
  (system + tools + last-message uses 3 of 4).

### Added — CLI (G5)

- **`fabri replay <session_id>`.** Re-runs the original task from a recorded
  trace against the *current* memory state. Prints a before/after summary
  (outcome, cost, steps) plus a JSON dump. Useful for "did the memory loop
  actually change behavior?" — but the LLM is non-deterministic, so read it
  as a directional signal and pair with `session_delta` for statistical
  weight.

### Added — reports (G7)

- **Per-step cost attribution.** `step_finished` events now carry a
  `cost_usd` field (priced from the step's `response.usage` alone), and
  `reports.aggregate` walks the trace step-by-step to split each step's LLM
  cost across the tools dispatched that step. v0.7.0's proportional-by-
  total-call-count split is the fallback for legacy traces without
  per-step cost.

### Added — MCP (HTTP transport, server side)

- **`MCPHttpClient`.** JSON-RPC over HTTP POST. Same surface as
  `MCPStdioClient` (`initialize` / `list_tools` / `call_tool` / `close`).
  No SSE streaming yet — that's the next follow-up. Server config gains
  `url` + optional `headers` fields:

      tools:
        mcp_servers:
          - name: fs
            url: "https://mcp.example.com/jsonrpc"
            headers: {Authorization: "Bearer ..."}

  `build_mcp_tools` picks transport by which field is set (errors loudly
  on both / neither).

- **`fabri.tools.mcp_server`** — expose a fabri agent as an MCP server over
  stdio. Run as `python -m fabri.tools.mcp_server --config agent.yaml
  [--tool-name fabri_agent]`. Exposes ONE tool whose input is `{task:
  string}`; the call invokes `run_agent` and returns the agent's final text
  in the standard MCP `content[]` shape with `isError` set by the run's
  `success` field. Lazy-inits the tool registry + store on first call so
  list-tools-only clients don't pay setup cost.

### Added — LongMemEval benchmark (G1 follow-up)

- **`fabri.benchmarks.longmemeval`** — full end-to-end runner.
  - HuggingFace dataset downloader (lazy, cached at `~/.cache/fabri/
    longmemeval/`). Falls back to a clear install hint if `datasets` isn't
    installed.
  - Per-case isolated memory collection so cross-case leakage doesn't
    inflate scores.
  - Exact-match scorer (case + whitespace normalized) shipped; LLM-judge
    scorer scaffolded behind `--judge`.
  - Per-category aggregation.
  - CLI: `python -m fabri.benchmarks.longmemeval --config agent.yaml
    --limit 10` (full eval is ~10k cases, several hours).

  **Status:** runner end-to-end + scoring helpers under test; the publish-
  worthy ~10k-case number needs a user-side run with real API credits.
  Single highest-leverage marketing artifact once it lands.

### Changed — memory/compress.py hardening (TODO.md)

- **Model-aware tokenizer.** `count_tokens` and `enforce_token_cap` now
  pick a tiktoken encoding per model (`o200k_base` for Claude 4.x and
  gpt-4o; `cl100k_base` fallback for unknown). The historical hard-coded
  `cl100k_base` could mis-count by ~10-15% on Claude.
- **Word-boundary truncation.** `enforce_token_cap` no longer slices a
  guideline mid-token — it backs up to the previous whitespace before
  appending `…`. Stops guidelines that end in a meaningless half-syllable.

### Tests

- **+19 tests** in `test_unit_v072_features.py` covering G7 per-step
  attribution + legacy fallback, G9 budget outcome + default, G21 message
  cache marking + non-mutation, tokenizer word-boundary + model-aware,
  MCP HTTP serialization, build_mcp_tools transport-picking rejection,
  MCP server initialize / list / unknown-method / notification handling,
  LongMemEval scoring + by-category aggregation. Suite 423 → 442.

### Deferred (need separate work)

- **MCP HTTP+SSE** — POST request/response works; streaming responses
  (the SSE variant) is the next follow-up.
- **`model_version` enforcement** in `memory/schema.py` — would invalidate
  existing collections; needs a migration story (rename collection on
  mismatch? raise + ask user to recreate?) before shipping.
- **Concurrent-ingest + wheel-packaging + multi-block tests** — TODO.md
  test-coverage holes. Tests-only changes; tracked for v0.7.3.
- **manifest_schema over-eager path rewriting** (`tools/manifest_schema.py
  :23`). Could affect ludexel's tool configs; needs a targeted test before
  changing.

## v0.7.1 — 2026-06-23

The "close the gaps that don't touch ludexel" release. P3 hardening pass +
six additive features from the P2 backlog. No agent-loop semantics changed.
No memory schema migration. No config-shape breaks. A host service that
uses fabri as a library (e.g. ludexel) needs zero changes.

### Added — fan-out telemetry & regret detection (G10/G11)

- **`subagent_*` fields in every `usage` event**: `subagent_count`,
  `subagent_successful_count`, `subagent_failed_count`,
  `subagent_max_subtree_cost_usd`, `subagent_regret_count`. Tells you
  whether the agent stayed single-threaded or fanned out, and what it cost.
- **`delegation_regret` trace event.** Fires when a `spawn_subagent`
  succeeded but the child ran ≤1 step *and* cost > $0 — i.e. the spawn was
  almost certainly inlinable. The event carries `tool`, `child_step_count`,
  `child_cost_usd`, and a `reason` string. The strategic "single-threaded by
  default" claim is now empirically falsifiable per-run.
- **`on_subagent_finished(call, ok, child_usage)` callback** in
  `_dispatch_tool_calls` — invoked once per spawn regardless of ok/failure.
  Optional + keyword-only so existing direct callers (the F2 timing tests)
  are unaffected.

### Added — CLI (G3, G14)

- **`fabri memory diff <session_a> <session_b>`** — partitions every
  guideline into `new in B`, `shared`, `only in A`. Demo-friendly: show what
  the agent *learned* in a 30-minute run.
- **`fabri tool init <lang> <name>`** — scaffold a new tool's manifest +
  executable stub in `python | go | node | bash`. Lands the pair under
  `--dir` (default `tools/agent_tools/`). Bash stubs are chmod 755.

### Added — polyglot examples & recipes (G12/G13/G15)

- **Rust example (`example_rust_tool/`)** — `regex_lines` tool: greps a file
  for a regex, returns matching lines. Cargo + serde + regex.
- **Node example (`example_node_tool/`)** — `file_stats` tool: bytes/lines/
  words + a language guess from the extension.
- **Tool recipes (`fabri.tools.recipes/`)** — copy-paste-ready patterns:
  `fetch_url`, `git_diff`, `grep_dir`, `run_shell_safe`, `python_eval`. Each
  ships with output caps + deny-lists where relevant.

### Changed — P3 hardening

- **`read_file` / `edit_file`** now refuse files > 1 MB with a clear error
  message pointing the agent at `outline_only` / line windowing. Stops a
  single tool call from blowing up the agent's context.
- **`decompose` parser strips ```json``` / ```toon``` / bare ``` fences**
  before json.loads / toon.decode — a fenced-but-otherwise-fine response is
  no longer misclassified as malformed.
- **`embed()` rejects empty/whitespace text** with a ValueError. Silent
  near-zero-vector dedup poisoning is gone.
- **Admin gate logs a WARNING when `FABRI_ADMIN_TOKEN` is unset** so an
  operator can grep their logs after deploy to verify auth is wired.
- **`build_tools` refuses a registry containing a tool named `decompose`**
  — that name is reserved for the framework meta-tool.

### Removed from the backlog (already fixed in an earlier release)

- OpenAI parallel-tool-call truncation. The OpenAI backend already collects
  every `message.tool_calls[]` and emits all of them on `LLMResponse`
  (`core/llm.py:473`). The TODO item is stale.

### Tests

- **+27 tests** across `test_unit_p3_hardening.py` (fence strip, empty embed
  reject, admin warning, reserved decompose, read_file cap),
  `test_unit_subagent_telemetry.py` (G10/G11 callback + regret event),
  `test_unit_tool_scaffold.py` (G14 scaffolder, all 4 languages),
  `test_unit_memory_diff.py` (G3 partitions). Suite 396 → 423.

### Ludexel compatibility

This release deliberately defers:
- G9 cost-budget enforcement (`agent.max_cost_usd`) — needs a UX design.
- G21 extended prompt caching — needs an opt-in flag and careful Anthropic
  testing.
- G5 trace replay — semantics are non-trivial (re-run against memory at
  point-in-time?).
- `model_version` enforcement — would invalidate existing collections.

## v0.7.0 — 2026-06-23

**The "make the claim true" release.** A strategic positioning review (see
`decks/internal/code-gaps.md`) identified ten gaps between fabri's pitch
("self-improving agent runtime with honest COGS") and the codebase. This
release ships all ten, end-to-end, with tests. The pitch is now demonstrable
in `fabri report` — not just instrumented under the hood.

### Added — observability (G6/G7/G8/G20)

- **`fabri report` CLI.** Aggregates `.fabri/traces/*.jsonl` into a usage
  report: total / by-model / by-tool cost, outcome distribution, per-session
  detail. `--since 7d/24h/30m` time filter, `--limit N`, `--format md|json|html`,
  `-o <file>` for write-to-file. Backed by a new `fabri.reports` module
  (`aggregate`, `render`, `chart`).
- **Cost-by-tool attribution (G7).** Proportional split of each session's
  `cost_usd` across its tool calls. Surfaced in markdown + HTML reports under
  "cost by tool" and via `SessionSummary.cost_by_tool`. A per-step attribution
  (LLM cost of step N → tools dispatched at step N) is a follow-up; this is a
  good-enough first cut.
- **COGS trendline chart (G8).** ASCII sparkline (`reports.chart.ascii_sparkline`)
  for the terminal output of `fabri report --since 30d`; self-contained SVG
  trendline (`reports.chart.svg_trendline`) embedded in the HTML report.
- **Static HTML report (G20).** `fabri report --format html -o report.html`
  writes a self-contained HTML file — no external CSS/JS, no fetches. Pastable
  into a deck/blog; seed of the eventual hosted dashboard.

### Added — memory observability (G2/G4)

- **`fabri memory show` / `fabri memory list`.** `show` is a human-readable
  listing of guidelines (filter by `--strategic` / `--tactical`, `--limit N`,
  `--markdown` output suitable for pasting into a deck). `list` is the
  pipeable JSONL counterpart. Backed by a new `QdrantMemoryStore.iterate()`
  (paginates Qdrant's scroll API) and a matching method on the new
  `SqliteMemoryStore`.
- **Guideline reuse rate metric (G4).** `retrieve_context_with_meta()` returns
  (text, meta) with `retrieved` / `from_prior_sessions` / `strategic` counts;
  the agent loop emits these in the `usage` event next to `cost_usd` as
  `guideline_reuse_rate`, `guidelines_retrieved`,
  `guidelines_from_prior_sessions`. "From prior sessions" =
  `hit_count >= 2 OR len(session_ids) >= 2` — the cross-session-learning
  signal, not just "memory had data."

### Added — embedded vector store (G16)

- **sqlite-vec memory backend.** New `fabri.memory.embedded_store.SqliteMemoryStore`
  with the same interface as `QdrantMemoryStore`. Selected via
  `memory.backend: sqlite` + `memory.sqlite_path: .fabri/memory.db`. Demos /
  CI / single-process deployments no longer require docker. Install via
  `pip install 'fabri[sqlite]'`. Production users keep Qdrant.
- **`fabri.runtime.build_memory_store(mem_cfg)` factory.** The agent loop, the
  CLI, and the benchmark harness all build their store through this factory;
  switching backends is a one-line config change.

### Added — benchmark harness (G1)

- **`fabri.benchmarks.session_delta` runner.** Runs the same task N times,
  records per-run cost / outcome / step count / guideline reuse rate, computes
  the cost delta between the first run and the median of the last three.
  CLI: `python -m fabri.benchmarks.session_delta --config agent.yaml
  --task "..." --runs 5`. Emits markdown + JSON under
  `.fabri/benchmarks/<ts>/`.
- **LongMemEval scaffold.** `fabri.benchmarks.longmemeval` directory in place
  with a porting plan in `README.md`. Dataset port is a follow-up
  (decks/internal/code-gaps.md G1).

### Added — MCP client (G19)

- **Minimal MCP stdio client.** `fabri.tools.mcp_client.MCPStdioClient` speaks
  JSON-RPC 2.0 over NDJSON (line-delimited). One server per process, server
  banner tolerance (skips up to 10 non-JSON lines), JSON-RPC error → tool_error
  conversion. `build_mcp_tools(server_cfg)` connects, lists, and wraps each
  remote tool as a `ToolManifest`. Config: `tools.mcp_servers: [{name, command, env?}]`.
- **`ToolRegistry.register_callable(manifest, handler, owns=...)`.** New hook
  for non-subprocess tools. MCP tools go through this path; agent-as-tool and
  manifest-discovered tools are unchanged. The `owns` reference keeps the
  backing MCP client alive for the registry's lifetime.

### Added — starter templates (G18)

- **`fabri init --template research|code-review|data-cleanup`.** Three vetted
  starter packs, each with a tailored `agent.yaml` (right max_steps, right
  planner mode, sqlite backend so no docker required) and 1–2 example tools
  (fetch_url for research, run_shell for code-review).
- **`fabri.scaffold.SCAFFOLD_TEMPLATES` registry** so future templates land
  by adding one dict.

### Changed

- **Default config gained `memory.backend` / `memory.sqlite_path` keys** —
  back-compat: omitted keys default to `qdrant` + the existing URL.
- **Default config gained `tools.mcp_servers: []`** — empty by default, MCP
  disabled.
- **`fabri init` accepts `--template`.** Default behavior (no flag) is the
  same as before — the existing hello-tool scaffold.

### Tests

- **+37 tests** across `test_unit_reports.py` (reports module: aggregation,
  markdown/json/html rendering, sparkline + SVG chart), `test_unit_mcp_client.py`
  (JSON-RPC framing, error handling, EOF detection, sanitization),
  `test_unit_scaffold_templates.py` (every template scaffolds + parses).
  Suite 359 → 396.
- Fixed `test_cmd_run_prints_synthesized_guideline_summary` (added in v0.6.1) —
  its helper was overriding `process_trace` after the test set it. The helper
  now accepts an `entries=` keyword.

## v0.6.1 — 2026-06-23

### Fixed

- **CLI no longer exits non-zero on `success_with_recovery` outcome.** The
  `fabri run` exit-code check compared `result["outcome"]` against the literal
  `"succeeded"`, which is not a value of the `Outcome` enum. Any run that
  recovered from a transient tool failure ended with `outcome="success_with_recovery"`
  and `success=True`, but the CLI still exited 1 — so host services that
  dispatch on the return code (e.g. ludexel's `runs` collection) mislabeled
  successful runs as failures and surfaced the agent's success summary as the
  error body. The check now positively matches `Outcome.SUCCESS` and
  `Outcome.SUCCESS_WITH_RECOVERY`.

## v0.6.0 — 2026-06-23

**License change: Apache-2.0 → Business Source License 1.1.** v0.6.0 and every
later release is BSL-licensed; free for individuals and for organizations with
≤ US $1M annual gross revenue, with a commercial license required above that
or when embedding fabri into a hosted/distributed product. Every BSL version
auto-converts to Apache 2.0 on **2030-06-23** (the Change Date). The historical
`COMMERCIAL.md` described that policy; it was removed when v0.12.0 restored
Apache-2.0 licensing retroactively.
Versions ≤ 0.4.6 remain Apache-2.0. v0.5.0 and v0.5.1 were withdrawn from
PyPI prior to general availability and are not supported; their functionality
is rolled into this release.

### Added (carried from withdrawn v0.5.0)

- **Per-run USD cost (COGS).** `LLMUsage` gained a `model` field (filled by both
  the Anthropic and OpenAI backends). New `fabri.pricing` module prices token
  usage per model (Sonnet 4.6, Haiku 4.5, Opus tier, gpt-4o; cache-write 1.25×,
  cache-read 0.10×; prefix-matches date-suffixed ids). `run_agent`'s `usage`
  trace event AND its return dict now carry `cost_usd` (this run's own tokens),
  `cost_by_model`, `subagent_cost_usd`, and `total_cost_usd`. An unknown/absent
  model prices to `None` (never a misleading 0).
- **Sub-agent cost rollup.** `agent_runner_tool` / `spawn_subagent` now return
  the child's `usage`; the parent's dispatch loop rolls each child's
  `total_cost_usd` into `subagent_cost_usd`, so a parent's `total_cost_usd` is
  the true end-to-end cost of itself **plus its whole sub-agent subtree**.
  Previously a sub-agent ran as a separate subprocess with its own trace, so its
  tokens were invisible to the parent and a fan-out run was massively
  undercounted. `_dispatch_tool_calls` gained an optional keyword-only
  `on_subagent_cost` callback (existing direct callers are unaffected).
- **Cache pre-warm.** `AnthropicLLMBackend.prewarm(system)` writes the static
  system+tools prefix into Anthropic's ephemeral cache via a `max_tokens=0`
  request and returns the call's `LLMUsage` (no-op on the scripted/OpenAI
  backends). Trims first-call latency; the cache-write itself is paid once
  either way, so fire it before a burst of same-prefix runs, not on a 24/7 loop.

### Added (carried from withdrawn v0.5.1)

- **Retry once on a `max_tokens` truncation before failing the run.** A single
  content-heavy turn (e.g. writing several files at once) previously hard-failed
  the entire multi-step run via `LLMError`. Both the Anthropic and OpenAI
  backends now retry that one step once at a higher cap (`min(max_tokens * 2,
  MAX_TOKENS_RETRY_CEILING)`, where the ceiling is 16000 — a non-streaming-safe
  bound) before giving up. We still fail loud if even the retry truncates, and
  never report a truncated answer as success. The discarded truncated attempt's
  tokens are folded into the reported `LLMUsage` so per-run cost stays accurate.
- **`QDRANT_URL` env override in `load_config`.** When `QDRANT_URL` is set in the
  environment, it wins over `memory.qdrant_url` from the yaml. A containerized
  host sets it once on the service; the orchestrator, the `spawn_subagent` tool,
  and every spawned child sub-agent inherit the env, so the reachable qdrant
  address (e.g. `qdrant:6333`) propagates across the subprocess boundary without
  rewriting each on-disk config. Fixes child sub-agents dying on connect when
  spawned with a `config_path` pointing at a repo yaml that still says
  `localhost:6333` (unreachable in-container). Never mutates the shared
  `DEFAULT_CONFIG`.

### Changed

- **Frugal-by-default base prompt.** `DEFAULT_AGENT_IDENTITY` is now
  deliberation-first. A `FRUGALITY_POLICY` is appended to **every** run (even
  when a domain config replaces the identity wholesale), plus registry-gated
  `DELEGATION_POLICY` (only when `spawn_subagent` is in the registry) and
  `CODE_ACTION_POLICY` (only when `python_exec`/`batch` is present). Together
  they steer the agent toward decisive calls over exploratory probing,
  single-threaded-by-default delegation, and code-as-action — grounded in
  CodeAct (arXiv:2402.01030), ReWOO (arXiv:2305.18323), Anthropic's multi-agent
  engineering post, and Cognition's *Don't Build Multi-Agents*.
- **`spawn_subagent` tool description** rewritten to gate delegation
  ("EXPENSIVE … spawn ONLY when a subtask is independent, parallelizable, and
  too large for your own context") and to document the new `usage` return field
  whose `total_cost_usd` rolls the subtree's cost up to the parent.

### Tests

- **+105 tests** (pricing edge cases, cost rollup across mixed/unknown models and
  sub-agent subtrees, both LLM backends incl. truncation-retry / prewarm /
  model-tagging / cache folding, the `QDRANT_URL` override, system-prompt
  frugality gating, and `spawn_subagent` command plumbing). Suite 246 → 351.

## v0.4.6 — 2026-06-22

### Changed

- **README is now self-contained for PyPI.** Previous versions linked to
  `docs/creating-an-agent.md` in the repo; the README now inlines the
  full config schema, tool manifest contract, agents-as-tools snippet,
  and library-usage example so PyPI readers don't depend on repo file
  visibility.

## v0.4.5 — 2026-06-22

### Changed

- **Tool-name word-boundary regexes are cached.** `retrieval._word_mentioned`
  was compiling `re.compile(rf"\b{name}\b", IGNORECASE)` on every retrieval
  call for every registered tool; the compiled patterns are now cached
  process-wide. Pure perf, no behaviour change.
- **`run_agent` skips materialising a `range(max_steps)` list when the
  planner already ran.** The legacy single-loop iterator is now a `range`
  rather than `list(range(...))`, and is empty when `plan_engaged` is true.
- **Inline-reasoning emit is centralised.** Four near-identical copies of
  the `thought` event-log block (executor / legacy × tool_calls / final_text
  branches) collapse to one `_emit_thought()` helper inside `run_agent`.

### Added

- **Protocol-error events now carry `had_tool_failure`.** When the LLM
  returns no tool calls and no usable final text, the `error` event and
  the paired log line include whether any prior tool call in the same
  item/run failed -- distinguishing "model is broken" from "model gave up
  after a cascade of tool failures" in post-hoc trace analysis.

## v0.4.4 — 2026-06-21

### Added

- **`spawn_subagent` accepts `memory_collection_suffix`.** Multi-domain
  orchestrators can now namespace each child's qdrant collection without
  cloning the spawned sub-agent's yaml. When set, the child writes to
  `<parent_collection>_<suffix>` (parent collection is read from the
  sub-agent's `memory.collection`); omitted/empty keeps the inherited
  behavior. The suffix is sanitized to lowercase `[a-z0-9_-]` and capped at
  32 chars so a host passing `Tile/Map.v2` doesn't fail with an opaque
  qdrant error. Hosts like ludexel can spawn `<parent>_character` vs
  `<parent>_map` from the same prompt template so cross-domain guidelines
  don't crowd each child's retrieval.
- **`EventType.DISCREPANCY` + `fabri.events.emit_discrepancy(...)` helper.**
  Hosts that post-hoc detect drift between what an agent claimed it did and
  what actually landed in their store now have a first-class trace event to
  emit (`{type: "discrepancy", path, reason}`). `process_trace` mines each
  discrepancy into a tactical guideline ("After write_file/edit_file at
  `<path>`, re-read the file in the same step to confirm the write
  persisted.") that flows through the existing dedup/promotion pipeline.
  `fabri traces show`/`tail` recognize the new event so it prints as a
  readable line, not the catch-all JSON fallback.



### Fixed

- **Silenced HuggingFace / sentence-transformers chatter on every run.** The
  embedding model used by memory retrieval (`all-MiniLM-L6-v2`) was leaking
  a tqdm `Loading weights` bar and an "unauthenticated requests to the HF
  Hub" warning to stderr on every `fabri run`. `memory/embeddings.py` now
  sets `HF_HUB_DISABLE_PROGRESS_BARS` / `TRANSFORMERS_VERBOSITY=error` /
  `HF_HUB_DISABLE_TELEMETRY` / `TOKENIZERS_PARALLELISM=false` **before**
  importing `sentence_transformers`, and pins the relevant loggers to
  WARNING/ERROR. A single `fabri` info line ("loading embedding model …")
  fires only on the very first download; cached loads are silent.
- **Skip the embedder entirely on a cold memory store.**
  `orchestrator/retrieval.py::retrieve_context` short-circuits when the
  Qdrant store has zero entries, so a fresh `fabri init` + first run never
  has to load the 44MB embedding model.

### Changed

- **`fabri traces show` / `tail` rendering.** Every event now carries an
  `HH:MM:SS (+Δs)` wallclock prefix (time "just works" by default, no
  flag). `thought` events render their full body — no 120-char truncation
  — with JSON pretty-printed and code-like blocks under a `┃` gutter.
  `tool_call` prints pretty-printed `args` and `result` payloads (capped
  at 40 lines; full payload still in the JSONL). `step_started` /
  `step_finished` get `── step N ──` separators. `llm_error` / `failed`
  print the full reason (no truncation).

## v0.4.2 — 2026-06-21

### Fixed

- **`ToolRegistry` import crash on annotation introspection.** The v0.4.0
  `invoke_batch(self, calls: list[dict])` signature shadowed `list` against
  the existing `ToolRegistry.list()` method — under PEP 649 deferred
  annotations, any consumer that touched `__annotations__` /
  `inspect.signature` / `typing.get_type_hints` on the class hit
  `TypeError: 'function' object is not subscriptable`. Fix:
  `from __future__ import annotations` at the top of `tools/registry.py`
  so all annotations stay as strings and the lookup never resolves
  `list` against the method. Public API unchanged (`registry.list()`
  still works).

## v0.4.1 — 2026-06-21

PyPI metadata polish: package description rewritten to surface the A1–A5
capabilities (planner/executor, retrieved tools, batch, success-pattern
mining, usage events) alongside the v0.3.x feature set. No code changes.

## v0.4.0 — 2026-06-21

Token-optimization series A1–A5 (planner/executor split, retrieved tool
descriptions, batch tool, success-pattern mining, per-run usage event). All
changes are non-breaking: defaults preserve the v0.3.0 behaviour and the new
paths are opt-in via `agent.planner.*`, `tools.retrieval.*`, or by listing
`batch` in `tools.enabled`.

### Added

- **Per-run `usage` event (A5).** `run_agent` now accumulates per-call
  input/output/cache-creation/cache-read token totals across the loop and
  emits a `usage` trace event at run end, alongside the existing
  `final` / `failed` / `incomplete` event. The same fields are returned in
  the `run_agent` result dict under `usage` (plus `step_count` and
  `wall_time_s`) so host services can persist per-run cost without parsing
  stderr logs. `LLMResponse` gained an optional `LLMUsage` carrier; the
  Anthropic and OpenAI backends fill it; `ScriptedLLMBackend` leaves it
  `None` and totals stay zero.

- **Retrieved tool descriptions (A1).** New
  `orchestrator.retrieval.retrieve_tools(task, registry, top_k, always_include)`
  ranks a registry's tools by cosine similarity of their descriptions to the
  task. When `tools.retrieval.enabled: true` is set in the config,
  `run_agent` narrows both the `Available tools:` block in the system prompt
  AND the provider's `tools=` list (via a new `LLMBackend.set_tools()`) to
  the top-K + an always-include set (defaults: `spawn_subagent`, `ask_user`,
  `decompose`). The filtered subset is fixed for the whole run so the v0.3.0
  prompt cache still hits across steps. Per-tool description vectors are
  cached at module scope so re-runs don't re-embed every tool.

- **Planner / executor split (A2).** New `core/planner.py` exports
  `plan(task, llm, max_items)` and `PlanItem`. `run_agent` gained
  `planner_mode: "off" | "auto" | "force"` (default `off` for back-compat),
  a `planner_llm` argument (with the historical `decompose_llm` kept as a
  fallback), and `planner_max_items` / `planner_auto_token_threshold`. When
  the planner is engaged, the executor runs one step-loop per plan item in
  dependency-resolved order with a minimal per-item user message ("current
  goal + artefacts + previously completed"), so each item pays only its own
  share of the prompt instead of the full accumulated history. New trace
  events: `plan_started`, `plan_item_started`, `plan_item_finished`,
  `plan_finished`. Configurable via `agent.planner.{enabled, mode,
  max_items, auto_token_threshold}`.

- **`batch` tool (A3).** A new built-in tool that takes
  `{"calls": [{"name": "...", "args": {...}}, ...]}` and dispatches each
  inside the registry process, collapsing the common
  "validate -> schema_check -> xref_check -> generator_dryrun" verification
  ladder from N model round-trips to one. Nested `batch` calls and
  side-effecting meta-tools (`spawn_subagent`, `ask_user`) are refused with
  a clear per-entry error rather than silently dispatched. Default off;
  opt-in by listing `batch` in `tools.enabled`.

- **Success-pattern mining (A4).** `process_trace` now also mines a "what
  worked" guideline from every run that ended with a `final` event and at
  least one ok=true tool call, ingesting it under a new
  `kind: "success_pattern"`. `MemoryEntry.id` is now namespaced by kind
  (success vs failure) so a success_pattern can't collide with a textually
  similar failure-derived guideline. `retrieve_context` reserves up to
  `top_k // 2` slots for success patterns so they survive even when a flood
  of failure-derived guidelines would otherwise drown them at retrieval.

## v0.3.0 — 2026-06-21

Token-optimization for file-generating agents. All changes are non-breaking:
existing configs and tool manifests keep working unchanged; the new behaviour
is opt-out (caching) or opt-in (read_file windowing/outline).

### Added

- **Anthropic prompt caching on the static prefix.**
  `AnthropicLLMBackend` now wraps the system prompt as a `cache_control:
  ephemeral` text block and tags the last entry in the tool list with the
  same marker — Anthropic caches every block at and before the marker, so
  the system prompt + tool descriptions are billed at ~10% of full cost on
  cache hits. The constructor accepts `enable_prompt_cache: bool = True` so
  cost-sensitive or test runs can opt out. `cache_creation_input_tokens` and
  `cache_read_input_tokens` are now logged on every call so cache wins are
  visible in run traces.

- **`read_file` supports windowed reads and structural outlines.** New
  optional args `line_start` / `line_end` (1-indexed, inclusive) return a
  slice with `start_line`, `end_line`, `total_lines`, `truncated`. New
  `outline_only: true` returns the file's top-level structure (def/class/
  heading/CONSTANT lines plus line numbers) for fast navigation before a
  targeted window read. Whole-file reads (no args) keep their pre-change
  output shape so every existing consumer is unaffected.

### Changed

- **Default agent identity steers toward `edit_file` over `write_file`.**
  When both tools are present in the registry, the system prompt now
  appends a `FILE_EDIT_POLICY` block telling the model to prefer surgical
  string-replace edits over whole-file rewrites, and to read file windows
  rather than whole files. The hint is registry-aware: it's skipped when
  the agent doesn't actually have `edit_file` available. This is the
  highest-ROI output-token cut for Ludexel-style file-gen workloads.

## v0.2.3 — 2026-06-21

### Fixed

- **`fabri run` now exits non-zero on a non-succeeded outcome.** When
  the agent ran out of steps, hit a provider error (rate limit, 5xx,
  malformed response), or produced no final answer, `cmd_run` was
  still returning silently — meaning the process exited 0 even though
  the trace was full of `failed` events. Host services dispatching on
  the exit code (like ludexel's run record) wrote the run as succeeded
  and lost the failure cause. Now: `sys.exit(1)` when
  `result["success"]` is False or `result["outcome"] != "succeeded"`,
  after the trace ingestion side-effects have run.

## v0.2.2 — 2026-06-21

Non-breaking: existing trace consumers ignore the new event kind, and
LLMResponse gains an optional field that defaults to None.

### Added

- **Agent reasoning surfaces in the trace.** When Claude returns a
  response with both `text` content blocks AND one or more `tool_use`
  blocks in the same turn, the inline reasoning text was previously
  dropped on the floor — `AnthropicLLMBackend.step` only captured the
  tool_use blocks. Now the text is captured onto
  `LLMResponse.thinking_text` and the agent loop emits a
  `{"type": "thought", "text": ..., "step": N}` event in the trace
  BEFORE the matching `tool_call` events. Host UIs can render the
  thought as the "Let me check existing characters first…" reasoning
  context that precedes the tool dispatch. Pure final responses
  unchanged (text still becomes `final_text`).

## v0.2.1 — 2026-06-20

Burns down the rest of Tracks F, S, and A from `docs/ROADMAP.md`. Nothing
breaking; consuming projects that already work on v0.2.0 keep working.

### Added

- **F1 — dynamic `spawn_subagent` builtin.**
  `src/fabri/tools/examples/spawn_subagent.{py,json}`. Parent agents now
  pick the sub-agent config at runtime, rather than the static
  `tools.agents[]` form where the choice is pre-baked at config load.
  Shells out to the same `agent_runner_tool.py` the static F0 path uses,
  so the subprocess contract is identical:
  `{final_text, outcome, session_id, trace_path}`. Input schema:
  `{config_path, task, system_prompt_inline?, system_prompt_path?,
  additional_context?, parallel_group?, timeout_s?}`.
- **F1 — runner system-prompt overrides.**
  `agent_runner_tool.py` gains `--system-prompt` / `--system-prompt-file`
  (mutually exclusive). Parents can override a sub-agent's configured
  prompt per call without editing its yaml.
- **A1 — `ask_user` builtin.**
  `src/fabri/tools/examples/ask_user.{py,json}`. Blocks on a clarifying
  question routed to the host via a Unix socket (production) or stdin
  (CLI dev). Question IDs make the socket transport safe for concurrent
  sub-agents — a misrouted reply errors instead of being silently
  accepted.
- **A1 — runner `--ask-user-socket=<path>`.**
  Available on `agent_runner_tool.py` and `fabri run`. Sets
  `FABRI_ASK_USER_SOCKET` in `os.environ`; tools inherit it directly, so
  no registry plumbing was needed (unlike `FABRI_SANDBOX_ROOT`, which is
  per-registry).
- **S1 — `fabri.sandbox` package.**
  `Sandbox` ABC with `run_tool` / `sync_in` / `sync_out` / `dispose`.
  `LocalSandbox` lifts today's `$FABRI_SANDBOX_ROOT`-based behavior into
  an object. `ToolRegistry` now routes every invoke through
  `self.sandbox.run_tool`; defaults to `LocalSandbox` so configs that
  never name a sandbox see no behavior shift.
- **F2 — parallel-aware dispatch.**
  `core/agent.py` indexes `spawn_subagent` calls by `parallel_group` and
  fans them out via `ThreadPoolExecutor`. Other tool kinds, and
  ungrouped spawn calls, stay serial. Assistant/user message blocks
  preserve original call order so the Anthropic API contract holds.
  `tool_call` trace events for parallel calls carry the `parallel_group`
  field for trace-tail viewers.
- **S2 — `DockerSandbox` + `Dockerfile.base`.**
  Pooled warm-container backend. Lazy fill on first acquire. Shells out
  to the `docker` CLI rather than depending on docker-py. State
  ferrying intentionally deferred to host-injected `sync_in_hook` /
  `sync_out_hook` callbacks — the framework owns container plumbing;
  consumers own data plumbing. `Dockerfile.base` ships under
  `src/fabri/sandbox/` and is included in `package-data` so an
  installed wheel can build `fabri/sandbox:latest` directly.
- **F5a — `fabri --version`.**
  Argparse `action="version"` reads installed wheel metadata via
  `importlib.metadata.version("fabri")`. No constant to drift out of
  sync with `pyproject.toml`.
- **`fabri traces` subcommand.**
  Homegrown observability spine (no Langfuse / Agnost SDK dep).
  `traces show <session_id>` pretty-prints a JSONL trace with relative
  timestamps and `parallel_group` tags; `traces tail <session_id>`
  follows a trace file like `tail -f`; `traces list` sorts recent
  sessions under `$FABRI_HOME/traces` by mtime.

### Changed

- `ToolRegistry.invoke` routes tool subprocesses through
  `self.sandbox.run_tool` instead of calling `tools.runner.run_tool`
  directly. Default sandbox is `LocalSandbox`, so the runner-level
  behavior is unchanged for callers who don't pass a sandbox.

### Tests

- Suite grew from 156 to 191 (35 new tests across F1, A1, F2, S1, S2).
- F2 timing tests use `_dispatch_tool_calls` directly to bypass the
  embedding-model warm cost in `run_agent`, so the concurrency
  assertions don't false-fail on a cold cache.
- `S2` ships a `FakeBackend` for unit tests and one real-Docker
  integration test that auto-skips when `docker info` fails (CI without
  Docker-in-Docker).

### Backlog remaining

- **F5b** — docs: builtin list + worked `spawn_subagent` recipe in
  README + `docs/creating-an-agent.md`. The features it would document
  are all shipped; this is a quality-of-life follow-up, not a blocker.

## v0.2.0 — 2026-06-20

First PyPI release. Burns down the entire P0+P1+P2 backlog from
`TODO.md` (correctness/security audit), plus the F0 sub-agent
ergonomics. See `TODO.md` and the v0.2.0 release notes (#1) for the
full list. Highlights:

- F0: per-`tools.agents[]` overrides (`model`, `max_tokens`,
  `qdrant_url`, `memory_collection`); `llm.decompose_model` for
  cheap-model decomposition; sub-agents return `{session_id,
  trace_path}` so parent traces point straight at failing children.
- TOON-encoded tool results to cut LLM token cost.
- Anthropic + OpenAI backends fully round-trip parallel `tool_use` /
  `tool_result` blocks.
- `max_tokens` truncation, empty LLM response, and API errors all
  surface as live outcomes instead of silent SUCCESS.
- Memory dedup matches across `tactical` + `strategic` kinds.
- Sandbox tools fail closed when `FABRI_SANDBOX_ROOT` is unset.
- Bundled tool manifests packaged via
  `[tool.setuptools.package-data]`.

## v0.1.0 — pre-release

Initial scaffold under the `agent_memory` name. Renamed to `fabri`
before any external consumer existed (R1). No published artifact —
`v0.2.0` is the first wheel on PyPI.
