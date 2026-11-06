# Distribution, connectors, and the ecosystem around the engine

This is one of a set of study docs written so Rushikesh can re-learn what was actually built in
fabri over the last two-plus weeks, rather than trusting the changelog headlines. This doc covers
everything *around* the core agent engine: how a person gets from `pip install fabri` to a running
multi-agent crew, how that crew gets templated and shared, how the demo gets hosted, how fabri
tries to get itself adopted inside other people's repos, the connector layer that lets agents touch
the outside world, and the two backend-correctness bugs (GPT-5 support, cache-token billing) that
had to be fixed before any of this was usable on OpenAI. It closes with the GTM framing against
paperclip-ai, since a lot of the distribution decisions were made in direct reaction to it.

---

## 1. Why this track exists

Fabri's core is an agent-execution engine: compile a YAML config into an agent, run it, get a
trace, learn from it. That's necessary but not sufficient — nobody adopts an engine with no
on-ramp. The distribution work answers a chain of practical questions that came up in order:

1. Someone installs `pip install fabri`. What's the first five minutes?
2. They want more than one agent working together — how do they get a working multi-agent
   "agency" instead of writing YAML from scratch?
3. They want to *browse* pre-built agencies before committing to one — where's the catalog?
4. They want to *show someone* a running agency without asking them to install anything — where's
   the public demo?
5. They want fabri to prove itself inside their own repo, not just in a demo — what's the
   self-improvement story made visible and reviewable?
6. Agents need to actually do things — post to Slack, open a PR, call a webhook — safely. What's
   the connector layer, and what stops an agent from being tricked into hitting an internal IP or
   leaking a credential?

Each of these became a shipped feature, roughly in this order, across versions 0.13 through 0.20.

---

## 2. The CLI + scaffolder (`fabri new agency`, `fabri examples`, `fabri studio`) — v0.14.x

**The problem.** Before 0.14.0, using fabri meant hand-writing an `agent.yaml`, wiring up
specialist configs by hand, and running a Vite dev server against a separately-started API server
to see anything. That's fine for the person who built the engine; it's a wall for anyone else,
and it's specifically a wall for *other AI coding tools* (Claude Code, Codex, Grok Build, Hermes)
that want to drive fabri programmatically rather than through a bespoke integration.

**What shipped (v0.14.0, tag `v0.14.0`, PyPI).**

- **`fabri new agency <name> --template bug-crew|changelog|blank --dest <dir>`** — scaffolds a
  working agency: parent config + specialist configs + a sandboxed `workspace/` fixture + a
  README, with all paths resolved correctly for how fabri actually looks things up at runtime.
  The scaffolding logic lives in `src/fabri/agency_scaffold.py`, which does placeholder
  substitution over template files (see `_write_template`).
- **`fabri studio`** — serves the bundled Studio UI and the run API from *one* same-origin server,
  no Vite proxy needed. `pip install "fabri[sqlite]" && fabri studio` gets you straight into the
  Company view. Static serving is opt-in (`cmd_studio` in `src/fabri/cli.py`), so the existing
  `fabri serve` command is unchanged; if the Studio assets weren't built into the wheel, the
  failure message is actionable rather than a stack trace.
- **`fabri examples`** (and `fabri examples --copy <dir>`) — example agencies ship *inside the
  wheel itself*, including dotfiles like `workspace/.gitignore`, with CI asserting no stray
  `.pyc` bytecode leaks into the package.

**The 0.14.1 bug fix (commit `7f55acf`).** Scaffolded agencies were all sharing one hardcoded
memory database — so two different agencies you scaffolded off the same template would read and
write each other's learned guidelines, which defeats the entire point of per-agency memory. The
fix threads a `__AGENCY_SLUG__` placeholder (`src/fabri/agency_scaffold.py:13`) through the
scaffolding templates so each new agency gets its own memory collection at creation time, not a
shared default. This is a small fix but it's the kind of bug that silently corrupts the "agents
learn from their own runs" story if it isn't caught — worth remembering as a class of bug to watch
for in any future templating/scaffolding work: **shared defaults where the whole point is
per-instance isolation.**

**Current state:** shipped, CI-green, live on PyPI. This is the actual on-ramp today.

---

## 3. The portable agency-builder skill layer

**The problem.** Once the CLI existed, the *procedure* for building a good agency (which template
to start from, how to set `tools.agents[]`, how sandboxing works, how to verify a repair loop)
still lived only in one Claude session's memory. It needed to be written down once and made usable
by any AI tool, not just Claude Code.

**What it is:** `skills/agency-builder/` in the fabri repo — one canonical, path-relative source of
truth, plus thin per-platform "briefs" at `skills/agency-builder/briefs/{codex,grok,hermes}.md`.
Each brief is short by design: it tells that tool to drive the *same public CLI* (`fabri examples`,
`fabri new agency ... --dest`, `fabri serve --config`, `fabri studio`) and explicitly says "do not
reimplement fabri scaffolding, serving, or Studio behavior" — the CLI is the integration surface,
not a tool-specific SDK. The skill also carries `references/agency-kernel.md`, `references/apply.md`,
and a fill-in `templates/agency-frame.md` for framing a new agency before building it.

This grew out of (and generalized) an earlier Claude-Code-only skill,
`~/.claude/skills/fabri-agency-builder/`, which was built first to scaffold, serve, and
browser-verify a small agency end-to-end (parent `agent.openai.yaml` + specialists with
least-privilege tool sets + `sandbox_root` pointed at `workspace/`). Two real example agencies came
out of that work: `examples/agencies/changelog-release-notes` (an "artifact crew") and
`examples/agencies/bug-triage-crew` (a "code crew" that localizes a bug, edits code, and runs the
test suite). Company-view info-flow animations and a cohesive lucide line-icon set (replacing
emoji glyphs) were added to Studio alongside this work (PRs #21–#23, fabri 0.13.2) so the crew was
actually pleasant to watch run.

**Current state:** shipped, in-repo. The Claude-Code-specific version still hardcodes
`/Users/rushour0/gba/fabri` in one place and was flagged to be genericized before it was ever
meant to be committed as a project skill — the in-repo `skills/agency-builder/` is the
already-genericized, portable descendant.

---

## 4. The fabri-rosters catalog repo (`fabri new agency --from`)

**The problem.** `fabri new agency --template X` only scaffolds from the handful of templates
shipped inside fabri's own wheel. There was no way to browse a *catalog* of agencies built by
other people, and no way to install one by reference the way you'd `npm install` a package.

**The decision (2026-07-18, user-driven).** Build a **separate repo**, `fabri-rosters`, that is
both (a) an installable agency-template registry and (b) a rendered static gallery — this is
explicitly fabri's answer to `paperclipai/companies` (see §8). Four decisions were made
up front: scope this pass to the rosters MVP (defer a Studio-native roster browser); support both
a fabri-authored showcase roster *and* user-composed ones; make the repo double as registry *and*
gallery rather than shipping two artifacts; and differentiate on fabri's own wedge (self-improving
memory + COGS-first + open engine) rather than trying to match paperclip's governance feature set.

**Scope, as it actually shipped.** The original MVP shape: `agencies/<name>/{agency.toml,
agent.openai.yaml, specialists…}` reusing the same `__AGENCY_ROOT__`/`__AGENCY_SLUG__`/`__RUN_FROM__`
placeholder contract that `agency_scaffold.py` already used, plus `rosters/<name>/roster.toml` for
a curated multi-agency "company," a generated `index.json`, and a static card-grid `site/`. The
fabri-side change is `fabri new agency --from <gh:|local path>`, which reuses the existing
placeholder-substitution machinery via a new `src/fabri/agency_registry.py` resolver.

**What it grew into.** The same day, the user expanded this into a full phased program — a
`company.toml` format for multi-level orgs with explicit `report_to` (compiling to nested
`tools.agents[]`, since fabri's runtime already supports recursive delegation for free — a child
config can itself declare `tools.agents[]`), `fabri serve --company` with `/companies`/`/agents`
endpoints, a Studio Companies view, an Agents dashboard, and eventually an "improve-agency-with-
builder" loop with a human-approval diff gate. Worth being honest about the scope creep here: this
is a six-phase program (P1–P6) sketched in one session off a one-day MVP ask, and only P1 (the
rosters registry + `--from`) is actually built and proven; P2 (`company.toml`) shipped in a
simpler form (see the connectors section below — `agentworks/company.toml` in fabri-rosters
compiles with a flat `ceo` + `bug_fix_crew` structure), and P3–P6 have not shipped.

**A companion pillar that got added mid-flight:** human-in-the-loop routing. The user wanted a
pending `ask_user` question to both appear in the Studio dashboard *and* notify the right person
via Slack, with a Slack reply resuming the run. That got scoped down live: Slack notification for
ask_user shipped (`routing.slack` config + `service/slack_notify.py`, non-fatal stdlib
`chat.postMessage`), but the user explicitly deprioritized Slack round-tripping in favor of a
dashboard-first experience — "keep things in a web dashboard first," "keep hitl very nicely
presented." What actually shipped instead was a cross-run pending-question inbox: `GET /questions`
on the backend and a `QuestionsInbox.tsx` surface in Studio (polling, source-agency label, age
indicator, options/free-text answer, empty state), browser-verified against a live server with
three real pending questions. Slack Socket Mode two-way answering and approver binding are
explicitly deferred, not built.

**Current state:** `fabri-rosters` is a real, separate repo at `/Users/rushour0/gba/fabri-rosters`
(referenced elsewhere as hosting both the Coolify demo and a GitHub Pages gallery). One real
company lives there: `companies/agentworks/company.toml` (renamed live from "scalex" to
"agentworks" — user's rationale: "name it what it does" — with a `ceo` role delegating to
`bug_fix_crew` which points at `../../agencies/bug-triage-crew`). Rosters CI has a real gotcha
worth knowing: it **requires** `python3 scripts/build_index.py` to be run and its output committed
alongside any company/agency change, or CI fails.

---

## 5. Coolify demo hosting — fabri.rushour0.com

**What it is.** The public, interactive Studio demo at `fabri.rushour0.com` (running `fabri studio
--catalog`) is hosted by **Coolify on the user's own AWS EC2 box** (ap-south-1, a single
long-lived instance; Coolify itself reachable at `coolify.rushour0.com`). Coolify builds
`deploy/Dockerfile` out of the **fabri-rosters** repo — not the fabri engine repo — which is worth
remembering because it's an easy thing to misremember (the user did, once, in-session).

**Deploy mechanism.** Not Fly.io, not SSH-and-restart. A GitHub Action just `curl`s Coolify's
deploy webhook (the URL is committed inline in `deploy-demo.yml`, since the URL alone isn't a
usable secret) with `Authorization: Bearer <COOLIFY_TOKEN>` from one repo secret, and Coolify pulls
and rebuilds. The user pushed back explicitly on reaching for Fly.io — the existing box + webhook
is the intended answer, not a platform migration.

**Three real operational bugs, each fixed live and each worth knowing if the demo ever looks
stale again:**

1. **Docker layer-cache staleness.** The Dockerfile's `RUN pip install "fabri[...]>=0.16.1"` layer
   was being cached across builds, so `force=true` redeploys kept shipping an old fabri version —
   the demo was still serving pre-blue-accent CSS long after 0.16.2/0.16.3 were on PyPI. First fix
   attempt: add `ADD https://pypi.org/pypi/fabri/json /tmp/fabri-pypi.json` immediately before the
   pip line, so its changing content busts the cache. **This fix turned out to be unreliable** —
   PyPI's JSON CDN can itself serve a stale response at build time, so the demo got stuck on
   0.17.0 while 0.18.0 was already published. The durable fix (pinning `fabri==<version>` in the
   Dockerfile, or an explicit build-arg cache-bust) is still marked TODO. The reliable way to
   *verify* which version is actually running is to SSH in and run `docker exec <container> pip
   show fabri`, not to trust the CSS hash.
2. **Disk-full.** The box has a 20GB disk. Because every release regenerates the fabri layer, Docker
   build cache piled up to ~7.7GB and filled the disk, causing deploys to fail fast (~40s in, before
   the heavy pip step — a "no space left on device" signature). Fixed live with `docker builder
   prune -af` (safe — reclaims build cache only, never touches running containers or volumes),
   recovering 9GB. A scheduled prune (cron or a Coolify cleanup schedule) is still TODO.
3. **Auth is live on the demo since 0.18.0** — `FABRI_AUTH_ENABLED=1` + `FABRI_AUTH_SECRET` set via
   Coolify's env API — so the whole public demo now sits behind a login wall. This is opt-in at the
   engine level (off by default); it was turned on specifically for the public demo.

**Current state:** live, but known to require periodic manual attention (disk, cache-bust
reliability). The gallery is a *separate* deployment — GitHub Pages, built from a React+Vite+
Tailwind app under `web/` in fabri-rosters, publishing to `rushour0.github.io/fabri-rosters`. Design
tokens (warm-dark `#0c0d10` background, one accent color, 10px radius) are shared between Studio
and the gallery, though note the accent color changed mid-project from gold (`#d7a13b`) to blue
(`#0d92f4`) and a later memory note references the tokens as gold again — treat the exact current
hex as something to verify live rather than trust from notes.

---

## 6. The repo agent — `fabri repo suggest-prompt` (v0.17.0)

**The problem.** Fabri's self-improving memory loop (mine a guideline from a trace → store it →
retrieve it on a later run → promote it once it recurs across sessions) was real, but entirely
invisible outside a fabri session. There was no way for someone maintaining a *repo* that uses
fabri to see, in their normal review workflow, that the agent had learned something and might want
that folded into its base prompt.

**What shipped.** A new `fabri repo` command group, GitHub-only in v1, authenticating via
`GITHUB_TOKEN` (deliberately Actions-native — no separate model API key, no server to run). The
client itself (`src/fabri/repo/github.py`) was lifted from a proven pattern in the `ludexel-app`
project (`service/github_issues.py`/`github_prs.py`): a plain `_http` Bearer-token caller, with
list-before-create dedup driven by a hidden `<!-- fabri:repo:<key> -->` marker in the issue/PR body
so re-runs comment on the existing issue instead of spamming duplicates.

- **`fabri repo issue`** — creates or updates a deduplicated tracking issue from a `--key`.
- **`fabri repo suggest-prompt`** — the actual wedge. It reads an agent's *promoted* `strategic`
  guidelines (lessons that have recurred across at least 3 sessions — see the promotion threshold
  below) and opens a GitHub issue proposing they be folded into the agent's system prompt. This is
  the self-improving loop made **visible and reviewable inside the repo**, not just inside a
  fabri run's private memory store.
- **`fabri repo open-pr`** (added in 0.18.0, alongside GitLab and Bitbucket provider adapters
  behind a common repo-provider interface) goes one step further than the issue: it minimally
  edits the raw `agent.yaml` text in a temporary clone — adding or replacing a clearly marked
  learned-guidelines block inside `system_prompt` — then opens or updates a draft PR. Critically,
  **the local working tree is never changed**; everything happens against a temp clone and a
  proposal branch. Provider is auto-detected from CI, or set explicitly with `--provider
  github|gitlab|bitbucket`; tokens come from `--token` or `GITHUB_TOKEN`/`GITLAB_TOKEN`/
  `BITBUCKET_TOKEN`.

`docs/repo-agent.md` in the repo has a copy-paste GitHub Actions workflow: checkout, install
`fabri[sqlite]`, run the agency once, then `fabri repo suggest-prompt --config agent.yaml` with
just `permissions: issues: write`. A live reference repo,
[Rushour0/fabri-repo-demo](https://github.com/Rushour0/fabri-repo-demo), files its own
self-improvement issue on a weekly schedule.

**What's actually proven, stated carefully (this matters for honest promo copy).** The self-
improving loop genuinely works end to end: mine → store → retrieve closes across two separate
sessions, and dedup + recurrence promotes a guideline from tactical to strategic once it's seen
across 3 distinct `session_id`s (similarity threshold 0.85, tracked in `pruning.py`). But **fabri
does not rewrite the base prompt itself** — it grows a retrievable guideline store that gets
injected into context at runtime; "self-improving" means *better retrieved context*, and quality
is bound by retrieval quality, not by literal prompt evolution. The claim "gets cheaper over time"
is explicitly **not benchmarked** — `BENCHMARKS.md`'s session-delta table was still "results
pending" as of this being verified, and later benchmark work (§9) found real evidence the cost
benefit doesn't show up on short, low-headroom tasks. The safe claims are: reports each run's real
COGS, learns from its own runs, and is open source. `suggest-prompt` was verified end-to-end by
seeding a promoted strategic guideline and confirming it read it and reached the GitHub API (the
run only failed on a deliberately fake token, i.e. auth, not logic).

**Current state:** shipped 0.17.0, merged PR #45, tag `v0.17.0`. GitHub path live-verified against
a real workflow; GitLab/Bitbucket adapters shipped in 0.18.0 but are provider-interface additions,
not independently re-verified live in the same way.

---

## 7. Connectors — SSRF guards, credential store, webhook/Slack/email recipes, GitHub via MCP

This is the layer that lets an agent actually reach outside its sandbox — post a Slack message,
call a webhook, send an email, or drive GitHub — and the layer where "agent does something in the
real world" has to be made safe by construction, not by prompt instruction.

### 7a. GitHub via MCP (config-only connector)

Documented at `docs/connectors/github-mcp.md`. The key design point: **GitHub is configuration,
not a new fabri tool.** Fabri's existing MCP client (`src/fabri/tools/mcp_client.py`) starts a
configured MCP server at agent-build time, discovers its tools via `tools/list`, and wraps each
discovered tool as `mcp_github_<remote_tool>`. The recommended setup runs GitHub's own official
`ghcr.io/github/github-mcp-server` container via Docker rather than a third-party bridge, passing
`GITHUB_PERSONAL_ACCESS_TOKEN` through Docker's `-e` flag. Two schema facts worth remembering
because they're easy to get wrong: `command` and `url` are mutually exclusive (stdio vs HTTP
transport) and there is **no separate `args` key** — the entire invocation, including all
Docker flags, is one `command` argv list. Also: do not put `${GITHUB_PERSONAL_ACCESS_TOKEN}` in
checked-in YAML expecting shell expansion — `yaml.safe_load` doesn't do that; the launching shell
must already have the variable exported before fabri starts, or the credential-store handle
mechanism must be resolved into a plain env var upstream (see 7b — MCP servers do not resolve
`secret_refs` themselves, a known gap).

The doc is explicit about the least-privilege PAT scopes to grant (Contents read/write, Pull
requests read/write, Metadata read-only by default; explicitly *not* Administration/Actions/
Workflows/Webhooks/Secrets) and about the operating posture: **propose, don't apply** — work on a
task-specific branch, never push to the default branch, open or update a PR, and leave merge to a
human plus branch protection. The doc is also honest that this has been verified against fabri's
own MCP client schema but **not yet live-verified** against the actual GitHub MCP server or a real
PAT — that's an open item, not a shipped-and-proven claim.

### 7b. Connector primitives (waves 1 + 4, 2026-07-23)

Built via a `codex exec` fan-out (one agent per file set), landing as four CI-green PRs
(fabri#76, #77, #79, and fabri-rosters#15). Gate: `uv run --frozen pytest tests/ -q` → 1388
passed, 1 skipped (requires `docker compose up -d qdrant` first).

- **`src/fabri/tools/security/ssrf.py`** — SSRF guards extracted out of `recipes/fetch_url.py`
  into a shared, public module (the recipe keeps thin `_validate`/`_host_is_blocked` delegating
  wrappers), so any new outbound-request tool gets the same protection against being pointed at
  internal/private IPs.
- **`src/fabri/tools/credential_store.py` + `secret_refs.py`** — resolves a `provider:handle`
  reference (e.g. `github:acme`) to the environment variable `FABRI_CRED_<PROVIDER>_<HANDLE>`.
  This is the mechanism referenced but not yet wired into MCP server env resolution (7a's open
  gap).
- **Recipe tools**: `webhook_post`, `slack_post`, `send_email` (each with a `.py` + `.json` spec
  + tests). The webhook recipe is SSRF-validated *including redirect re-validation* (so a
  webhook target can't 302 its way around the check). Slack reuses `slack_notify`'s existing
  endpoint/auth shape. SMTP credentials are a single JSON secret shaped `{"user", "password"}`.
- **`docs/connectors/github-mcp.md`** itself was written in this wave.
- A `docs/memory.md` deep-dive (380 lines) and a README patch covering rosters/`--from`/Studio
  surfaces/memory tiers were also landed here.

**The stdlib `secrets` shadowing trap — worth remembering as a general lesson.** The credential
module was originally named `src/fabri/tools/secrets.py`. That collided with Python's *own*
stdlib `secrets` module, because subprocess-based recipe tools put their own directory first on
`sys.path`, so when `litellm` internally did `import secrets`, it silently picked up fabri's file
instead of the real stdlib module. The failure mode was ugly and indirect: `agent_runner_tool`
died with **empty stdout**, no traceback pointing at the real cause, and two tests went red. Fixed
by renaming the module to `secret_refs.py`. The generalizable rule: **any module that lives in a
directory whose scripts get run directly (so that directory lands on `sys.path[0]`) must not
collide with a stdlib module name** — before naming a new module in a `tools/` or `recipes/`
directory, check `python -c "import <name>"` resolves to the real stdlib, not your new file.

**Live reuse experiment (Wave 2, same session, $0.14 of a $20 authorized cap, model
`gpt-5.6-terra`).** Three Studio runs against the bug-triage crew with a seeded divide-by-100
discount bug. Run 1 fixed the bug fresh ($0.049). Run 2 was confounded — the shared workspace
already had the fix applied and the reuse metric couldn't distinguish "already fixed" from "no
memory used," so it read reuse 0.0. Run 3 re-seeded the bug fresh on the *same* server session and
got **reuse_rate 1.0** — the crew actually retrieved the divide-by-100 lesson from memory and
fixed the bug again, correctly. Two real bugs were found and fixed live in the process of getting
this clean result (both merged in PR #79):

1. `from_prior_sessions` in `orchestrator/retrieval.py` required `hit_count >= 2` across 2
   sessions before counting a guideline as cross-session reuse — which meant the *first* instance
   of cross-session reuse always read as 0.0, undercounting the metric by construction. Fixed to
   count entries whose provenance names another session.
2. `fabri studio --company` compiled agencies into a `mkdtemp` temp directory and baked crew-role
   memory's `sqlite_path` inside that temp tree — so the sharpest, most useful crew-level lessons
   died on every server restart, while only company-root memory survived. Fixed so all sqlite
   memory paths anchor at `<run_from>/.fabri/` instead of the ephemeral compile directory.

**Honest bottom line on this experiment:** reuse is now real and demonstrated live (1.0 on run 3),
but the *cost benefit* of that reuse is still unproven — this was a 2-step task with no headroom
to save, which is consistent with what the separate benchmark work (§9) found independently. Don't
conflate "memory gets retrieved and used" with "memory makes runs cheaper" — they're different,
separately-verified claims.

Two more fixes landed late in the same session (PR #80, merged): `AGENT_MEMORY` became a real
`<AGENT_MEMORY>...</AGENT_MEMORY>` XML-style tag with regex-tolerant leak stripping, because a
malformed leak (`<!-- AGENTMEMORY -->`) was actually reaching end users; and `company_org()` now
expands crew "leaves" into individual member-role nodes so Studio's org chart correctly shows
`ceo → crew → {triager, fixer, tester}` instead of collapsing the crew into one opaque node.

**Known open gaps, stated plainly:** MCP servers still don't resolve `secret_refs` in their `env`
block (a real gap between the two mechanisms in 7a/7b — flagged as a future card, not fixed).
Stripe billing integration is deferred, pending a Studio billing-markup feature that doesn't exist
yet. A test (`test_manifest_command_survives_a_path_with_no_working_python3`) is known-flaky,
PATH-dependent.

### 7c. Slack mention connector (`docs/slack-connector.md`)

A separate, simpler Slack integration for driving whole agency runs from Slack rather than just
notifying about `ask_user` questions: set `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET`, enable
`routing.slack.enabled` + `routing.slack.events_enabled`, subscribe a Slack app to `app_mention`
and `message.channels`, point the Events API request URL at `/slack/events`. `@mention`-ing the bot
in a channel runs the configured (or default) agency and posts progress + final result back into
the thread; a crew's `ask_user` question posts into the run's own Slack thread and a reply there
answers it. This is documented as working for mention-triggered runs; posting a *non-mention* run's
final result back to an "owned channel" thread is explicitly called out as a later follow-up, not
yet done.

---

## 8. GPT-5 / OpenAI support and the cache-token billing fix

Neither of these is "distribution" in the growth sense, but both were **load-bearing prerequisites**
for everything above — the demo, the rosters examples, and the repo-agent workflow all run on
OpenAI models, and both bugs would have silently broken or silently overcharged every one of them.

### 8a. GPT-5 support (shipped 2026-07-14, branch `fix/outcome-trust-gap`, commit `8ff29e4`)

Two real backend bugs were blocking **all** GPT-5 use on the OpenAI backend
(`src/fabri/core/llm.py`):

1. The backend hardcoded the `max_tokens` parameter, which GPT-5 and o-series models reject
   outright with a 400 error — they require `max_completion_tokens` instead. Fixed with a
   `_requires_max_completion_tokens` check that picks the right parameter for direct-OpenAI GPT-5/
   o-series while keeping `max_tokens` for classic models and OpenRouter passthrough.
2. GPT-5 reasoning models reject function tools on the `/v1/chat/completions` endpoint unless
   `reasoning_effort='none'` is set (the alternative, tool use via the Responses API, isn't
   something fabri speaks). Fixed by passing `reasoning_effort='none'` whenever GPT-5 is used with
   tools.

Alongside this, `src/fabri/pricing.py` was refactored to make **litellm's `model_cost` map the
primary pricing source** per-lookup, with the old hardcoded `PRICING` dict kept only as a
fallback (now including GPT-5 family rates matched to litellm's numbers). A real gotcha was found
in the process: `import litellm` triggers `load_dotenv()` internally, which silently pulls
whatever's in the current working directory's `.env` into `os.environ` — fabri itself never
auto-loads `.env` and resolves API keys explicitly, so `pricing.py` now snapshots `os.environ`
around the litellm import and discards any keys the import silently added, to avoid surprising
credential leakage between processes.

Role-model defaults were set for cost-effectiveness (user-approved as "good enough," not
scientifically tuned): manager/orchestrator → `gpt-5.6-terra` ($2.50/$15 per Mtok), specialists →
`gpt-5.6-luna` ($1/$6), and the cheap narrator role → `gpt-5-nano` ($0.05/$0.40), changed from the
previous `gpt-4o-mini` default in `src/fabri/core/config.py`.

Verified live end-to-end: the `changelog-release-notes` agency run on real OpenAI, deliverable
independently verified `{"ok": true}`, on both `gpt-4o` and `gpt-5.6-terra`, at roughly $0.02/run
by fabri's own cost estimate.

### 8b. Cache-token billing fix — an ~11x overcharge (fixed + merged PR #14, 2026-07-15)

**What went wrong.** `pricing.cost_for` in `src/fabri/pricing.py` charges cache-read tokens at
0.10x the normal input rate, *on top of* the full `input_tokens` count — an approach that only
works correctly if `input_tokens` already **excludes** cached tokens, which is how Anthropic and
Bedrock report it. OpenAI and Gemini report it differently: OpenAI's `prompt_tokens` *includes*
`prompt_tokens_details.cached_tokens`, and Gemini's `prompt_token_count` *includes*
`cached_content_token_count`. Because fabri's llm backend for both providers was passing that
cache-inclusive count straight through as `input_tokens` while *also* setting
`cache_read_input_tokens`, the cached portion of every prompt was being billed at roughly **1.10x**
the input rate instead of the intended 0.10x — an ~11x overcharge on the cached slice of a
request, translating to about a 3x overcharge on a realistically warm 80%-cached turn. This is
real money: `_deduct_run_cost` bills `total_cost_usd` directly against user credits, and the bug
went live specifically when the project switched from Anthropic to OpenAI.

**The fix.** Both the OpenAI backend (`src/fabri/core/llm.py`, ~line 575) and the Gemini backend
(~line 802) now subtract the cached-token count out of `input_tokens` before it's billed, so the
non-cached portion is what actually gets the full input rate. The OpenAI backend also fixed a
smaller adjacent bug: a discarded, truncated attempt's cached tokens were being dropped entirely
instead of folded into `cache_read`. Anthropic and Bedrock were already correct and are unchanged.
The invariant is now spelled out explicitly in `pricing.py`'s docstring so a future backend author
doesn't reintroduce it: "input_tokens EXCLUDES cached tokens; a new backend MUST normalize to this
or cached tokens bill at ~1.10x."

**How it was caught.** Two *existing* tests had literally encoded the bug as expected behavior —
`test_openai_cached_tokens_map_to_cache_read` asserted `input_tokens == 50` when 30 of those were
cached (should have asserted 20), and a Gemini usage-parsing test asserted `== 12` with 3 cached
(should have asserted 9). Both were corrected, and the OpenAI test was strengthened with a
hand-computed dollar proof that cached tokens now bill at 0.10x, not 1.10x, plus a new regression
test for the truncated-attempt-with-cache case. This is a good example of a bug that unit tests
had actively been protecting rather than catching — worth remembering that a green test suite
doesn't mean the *assertions* are right.

**A deliberately-left footgun.** `pricing._best_prefix_key` does longest-prefix matching to resolve
model ids, which correctly handles date-suffixed ids like `gpt-5.2-2025-12-11` → `gpt-5.2`, but
also means a typo like `gpt-5.9-doesnotexist` silently resolves to the `gpt-5` price tier instead of
returning `None` — a "non-None" sanity check would not catch that typo. This was deliberately left
alone because tightening the match risks breaking real date-suffixed ids; the mitigation in place
is that pricing tests assert exact dollar values rather than just non-None, and the plan is to
revisit only if this actually causes a real mispricing incident.

---

## 9. Release cadence, 0.13 → 0.20

Reading straight from `CHANGELOG.md`, the shape of roughly two weeks of releases:

- **0.13.0** (2026-07-15) — the cache-token billing fix; `fabri serve` gains cancel/history/fleet
  fan-out; per-model COGS surfaced on the result envelope; Studio grows into a three-surface
  conversational + observability UI.
- **0.13.1–0.13.2** (2026-07-18) — Studio's Company (agency-graph) view, a fix for reading the
  nested tool-result envelope correctly, line-icon set, info-flow animations, the bug-triage-crew
  example.
- **0.14.0–0.14.1** (2026-07-18) — the CLI/scaffolder distribution story (§2), and the
  `__AGENCY_SLUG__` per-agency memory isolation fix.
- **0.16.2–0.16.4** (2026-07-19) — Studio polish: URL-tracked tabs, a stable-width frame fix, the
  gold→blue accent change, then a depth/micro-motion pass.
- **0.17.0** (2026-07-20) — the repo agent (§6): `fabri repo suggest-prompt`/`issue`.
- **0.18.0–0.18.5** (2026-07-20) — `fabri repo open-pr` + GitLab/Bitbucket adapters; opt-in
  Studio auth (email+password, scrypt-hashed, HMAC-signed HttpOnly cookies, per-run ownership);
  Studio auth hardening; durable company memory (a compiled company gets an explicit root
  institutional-memory collection and records a postmortem per company task); self-improvement
  integration test coverage; the first bounded company setup-qualification benchmark work
  (Support HQ baseline: 3/3 fresh runs passed a frozen deterministic rubric).
- **0.19.0–0.19.4** (2026-07-21 to 2026-07-22) — this is the memory-benchmark-rigor stretch:
  automated company memory-vs-control study infrastructure, self-generated reproducibility
  manifests, a re-measurement against a *true* control (36 arms, $4.82) that found **no
  significant memory effect on quality or cost on any of three companies** — both the earlier
  "memory hurts" headline and an even earlier "+100pp memory wins" headline turned out to be
  scoring artifacts, not real effects. 0.19.3 tried a "hard output contract" prompt-wording fix
  for a dropped `AGENT_MEMORY` block; 0.19.4 **reverted it** four days later because live
  benchmark smokes showed the harder wording didn't increase block emission at all (0 of 2
  training runs) and caused a new structured-output regression (models embedding the memory block
  inside JSON response values, failing schema validation in 3 of 4 arms). The conclusion drawn
  from this reversal — memory capture is moving **engine-side** rather than continuing to rely on
  the model's prompt compliance — is a real strategic pivot, not a footnote.
- **0.20.0** (current, "release: cut 0.20.0 — connectors, convention mining v1, memory reuse
  fixes") — bundles the connector waves (§7b) and convention-mining work together with the memory
  reuse fixes.

**The honest throughline in the 0.18–0.19 stretch:** a genuinely significant fraction of this
period was not feature-shipping but *finding out that earlier positive memory results were
measurement bugs*, and then reverting a prompt-engineering fix that didn't survive its own live
verification. That's a real and useful negative result — it's the reason the project's public
claims about memory (§6) are phrased so carefully ("better retrieved context," not "gets cheaper
over time").

---

## 10. The GTM framing: fabri vs paperclip-ai

Researched 2026-07-18–20. `paperclipai/paperclip` is an open-source (MIT, reported ~20k–70k GitHub
stars depending on source) Node.js server + React dashboard that markets itself as "hire AI agents
to run your company." Its actual mechanism: define a mission, hire agents into an org chart, set
hard per-agent monthly budgets, run on scheduled heartbeats (wake → check tickets → act → report
up), keep persistent agent state across heartbeats, and provide board-level governance — approve
hires, override, pause/terminate, rollback. It's multi-tenant and, critically, it **wraps any
external agent CLI** (Claude Code, Codex, Cursor, Gemini) as a black box — "if it can receive a
heartbeat, it's hired."

**The layer distinction that the whole positioning rests on:** paperclip is a **control plane**
that schedules and governs black-box agents; fabri is the **engine** that actually runs the agent
loop, with per-role model selection and a self-improving memory loop. Paperclip's wrapped agents
can't learn the company's specific work because they're opaque to it; fabri's agents can, and this
was demonstrated live (reuse rate 0 → 0.67 across sessions, average steps 5 → 4, from earlier
benchmark work). Paperclip *caps* spend as a safety mechanism; fabri *reduces* spend as the goal
and prices real per-run COGS as a first-class feature.

**Explicit user decision on positioning:** do not compete on governance or org-chart features —
paperclip's home turf. Lead instead with what paperclip structurally cannot offer because its
agents are opaque to it: the self-improving memory loop, COGS-first unit economics baked into the
agency contract, and being an open engine you build products *on* rather than a platform you merely
operate inside.

**Does fabri-rosters equal paperclip?** Only the catalog half. Rosters (browse/scaffold agencies,
`fabri new agency --from`) is roughly paperclip's "hire from catalog" seed step, and Studio's
Company view is a partial analog of paperclip's dashboard. What's still genuinely missing for
governance-parity, stated plainly: run-24/7 scheduling, rolling (not just per-run) budgets, and a
real governance/approval runtime beyond hard per-session cancel. A five-slice build plan was scoped
(durable run store → self-improving dashboard trend view → rolling budgets → scheduler/heartbeats →
governance), deliberately ordered so the two differentiator slices (durable store, self-improving
dashboard) ship *before* the parity-chasing slices. Slice 1 (a real sqlite-backed `RunStore`,
persistent by default at `~/.fabri/serve`, replacing an incidental `index.jsonl` in a temp
directory) shipped as PR #48. Slice 2, the actual self-improving-dashboard UI consuming the new
`GET /agencies` cost-per-run and reuse-rate series, was scoped but its shipped state wasn't
confirmed in the sources reviewed for this doc — worth checking the live Studio build before
citing it as done.

---

## 11. What to actually trust, if someone asks "does this work"

A short, deliberately blunt summary of proven vs. claimed, gathered from the honest caveats
scattered through the notes above:

- **Proven live:** the CLI scaffolder + Studio same-origin serving (0.14.x); per-agency memory
  isolation after the slug fix; the repo-agent's mine→store→retrieve→promote→suggest-prompt loop
  across real sessions; the GPT-5 backend fixes (verified against real OpenAI runs); the
  cache-token billing fix (hand-computed dollar proof); the connectors' SSRF+redirect validation
  and the live Wave-2 reuse experiment (reuse_rate 1.0 on a re-seeded bug).
- **Shipped but not independently re-verified in the same rigorous way:** GitLab/Bitbucket repo
  providers, GitHub-via-MCP (explicitly marked not-yet-live-verified in its own doc), the P2–P6
  phases of the rosters program, Slack round-trip answering.
- **Actively disproven / reverted:** the earlier "memory hurts" and "+100pp memory wins" headlines
  from prior benchmark passes (both scoring artifacts, corrected in 0.19.2); the 0.19.3
  hard-output-contract prompt fix (reverted in 0.19.4 after live smokes showed it didn't work and
  introduced a regression); "gets cheaper over time" as a general claim (not benchmarked, and the
  live Wave-2 connector experiment independently found no cost benefit on a short, low-headroom
  task — consistent, not contradictory, with the benchmark team's own finding).
- **Known operational fragility:** the Coolify demo's Docker cache-bust mechanism is unreliable
  and needs a durable fix (version pinning) that hasn't been done; the demo box needs periodic
  manual `docker builder prune`.
