# Implementation brief — make ONE real repo run true, end to end

**For:** an implementation agent (Codex exec, or a fabri sub-agent). Claude verifies
every claim this agent makes against real artifacts — a "green" report is not proof.

**Repo:** `/Users/rushour0/gba/fabri` (engine + connectors + CLI). Agency preset lives
in `/Users/rushour0/gba/fabri-rosters`.

---

## 0. Mission (one sentence)

Prove **one** real software workflow end to end against live services: a **Linear**
ticket becomes a real fix that lands as a **GitHub** pull request and is announced in
**Slack** — driven by a fabri engineering agency, with the whole run recorded as a
committed artifact.

The point is **truth, not surface area.** One recorded live run beats ten new
connectors that only pass mock tests.

---

## 1. Honest starting state (do NOT rebuild these — extend them)

Verified in the current tree, so scope precisely:

| Piece | State | Where |
|---|---|---|
| GitHub REST provider | **Real, mock-tested only.** Has `open_or_update_issue`, `open_or_update_pr` (POST/PATCH `/pulls`), `push_branch` — but `push_branch` pushes a **single file** via `push_branch_with_url`/`token_url`. | `src/fabri/repo/github.py`, `src/fabri/repo/base.py` (`RepoProvider` Protocol) |
| Slack post | **Real, mock-tested only.** `post_slack_message` → `chat.postMessage`. Wired only into HITL ask_user routing; **not** an agent tool or a run step. | `src/fabri/service/slack_notify.py` |
| Linear | **Does not exist.** Net-new. | — |
| Credential store | `provider:handle` → env `FABRI_CRED_<PROVIDER>_<HANDLE>`. `resolve_secret(ref, store)`. Env-var backend only; `CredentialStore` Protocol exists for future backends. | `src/fabri/tools/credential_store.py`, `src/fabri/tools/secret_refs.py` |
| **Bot/OAuth setup (already built)** | A throwaway helper creates the **Slack bot**, **GitHub App (bot)**, and **Linear OAuth** app, runs their OAuth flows, and writes creds into a gitignored `.env.fabri.local` in the exact `FABRI_CRED_*` names. Consume these — do not re-invent cred capture. | `scripts/setup_bots.py`, `docs/repo-run-setup.md` |
| SSRF guard | Real: scheme allowlist, blocks private/loopback/metadata, revalidates redirects. Reuse it for every outbound call. | `src/fabri/tools/security/ssrf.py` |
| Tool wiring | Agencies enable builtin tools by name (`tools.enabled: [...]`, `manifest_dir: [builtin]`). Callable tools register via `registry.register_callable(manifest, handler)`. Tool results MUST normalize to `{ok, result?, error?}`. | `src/fabri/runtime.py:250` `build_tools`, `src/fabri/tools/registry.py`, `src/fabri/tools/manifest_schema.py` |
| `fabri repo` CLI | Only `suggest-prompt` (files a guideline issue). **No** repo-run starter. | `src/fabri/cli.py:622,1608` |
| Engineering agency | `bug-triage-crew` exists (manager + triager/fixer/tester, tools `read_file`/`edit_file`/`bash`), but its recorded run **faked the deliverable** ("Test Status: Not run … $0.00"). It runs inside a toy `workspace/` fixture, not a real repo. | `fabri-rosters/agencies/bug-triage-crew/` |

**Anti-goals learned from the audit** (do not repeat these failures):
- No connector in this repo has ever been proven against a real service — **every test is
  mock-only.** Your job is to change that for this one path.
- The engineering crew previously returned a plausible **writeup instead of doing the
  work.** A run that edits no files, opens no PR, and posts no message is a FAILURE even
  if the model says "done." Outcome ≠ artifact.

---

## 2. Target walking skeleton (the one flow to make true)

```
Linear issue  ──fetch──►  task text
      │                        │
      │                 fabri engineering agency runs on a REAL git checkout
      │                        │  (read → edit → run tests, in a real repo clone)
      │                        ▼
      │                 tests pass  ──►  git branch + commit (multi-file)
      │                                        │
      │                                   GitHub: open PR  ──► PR url
      │                                        │
      ├──comment on the Linear issue with the PR url ◄────────┘
      └──Slack: post "PR opened for <ISSUE>: <url>"  ◄─────────┘
```

New entrypoint (the "decent setup" the user asked for):

```
fabri repo run --from-linear <ISSUE-ID> --repo <owner/name> [--base main] [--config <agency.yaml>]
```

It must: resolve creds → fetch the Linear issue → clone/checkout `--repo` into the run
workspace → run the engineering agency against that checkout → on a **verified** pass,
push a branch + open a PR → comment the PR url back on Linear → post to Slack. On failure
at any gate: **fail closed**, report which gate failed, open no PR, post nothing false.

---

## 3. Build order (dependency-first vertical slice)

**Slice 1 — Linear connector (net-new).**
- `src/fabri/integrations/linear.py` (new package). Linear GraphQL API
  (`https://api.linear.app/graphql`). Auth: OAuth access token from `setup_bots.py`,
  header `Authorization: Bearer <token>` (personal API keys use the bare token — support
  both). Token resolved via `resolve_secret("linear:default")`.
- Functions: `fetch_issue(issue_id) -> {id, identifier, title, description, url, state}`,
  `comment_issue(issue_id, body) -> comment_url`, `set_state(issue_id, state_name)` (optional).
- Route every request through the SSRF guard.
- Mock unit tests + an env-gated live smoke (see §5).

**Slice 2 — GitHub App auth seam + real branch from a checkout + PR.**
- **Auth first — add a `GitHubAuth` seam** so the provider never knows how it got its
  token: `get_token() -> str`. Two impls, same interface:
  - `AppAuth` (primary, the bot / Vercel model): mint an RS256 **JWT** from the App id +
    private key (`FABRI_CRED_GITHUB_APP_ID` / `_PRIVATE_KEY` written by `setup_bots.py`),
    exchange at `POST /app/installations/{FABRI_CRED_GITHUB_INSTALLATION_ID}/access_tokens`
    for a ~1h installation token, cache + refresh. **This is the only place crypto is
    needed** — add `cryptography`/PyJWT as a scoped dep here, nowhere else.
  - `PatAuth` (fallback for a fast first smoke): returns a fine-grained PAT from
    `resolve_secret("github:default")`. Optional — skip if App auth lands cleanly.
- Then extend `repo/github.py` (or add `repo/git_local.py`) so a **multi-file** commit on a
  real local checkout is pushed to a new branch, then `open_or_update_pr` opens the PR.
  Reuse local `git` for the commit and the existing `push_branch_with_url`/`token_url`, but
  source the token from `GitHubAuth`. Do **not** regress the single-file `push_branch`.
- Live smoke: mint an installation token, push a branch to the installed repo, open+close a
  real PR.

**Slice 3 — Slack notify step/tool.**
- Expose `post_slack_message` as (a) a post-run notify step callable from the repo-run
  harness, and/or (b) a builtin agent tool (`slack_post`) registered via
  `register_callable` with `{ok,result,error}` results and `slack:<handle>` cred resolution.
- Live smoke: post one real message to a test channel, assert the returned `ts`/permalink.

**Slice 4 — `fabri repo run` orchestration.**
- New CLI subcommand under the existing `repo` parser (`cli.py:1608`). Stitches slices 1–3
  around a real agency run. Each hop is a **named gate** with fail-closed behavior.
- Persist a run record + the connector call results into the run trace.

**Slice 5 — make ONE engineering agency real.**
- Point `bug-triage-crew` (or a focused new `repo-fix-crew`) at the **cloned checkout** as
  `sandbox_root` instead of the toy fixture. Its `tester` must actually run the repo's tests
  and its result must gate the PR. Update its prompts so it cannot "illustratively" skip work.
- Delete/quarantine the faked-deliverable behavior — the crew either does the work or reports
  a real failure.

**Slice 6 — Multi-tenant "install anywhere" (PRODUCTIZATION — do NOT start until Slice 1–5
land one recorded live run).** Turns the single bot into the Vercel model where any user
installs the public fabri App on their own repos:
- Flip the App to **public** (`setup_bots.py … --public`, or in the App settings). One App,
  many installations; the PEM becomes a **fabri server secret**, not a local `.env`.
- **Hosted callback + webhook** on the existing service HTTP server (which already serves
  `/slack/events`, `http_server.py:241`): add `/github/setup` (post-install redirect →
  capture `installation_id`) and `/github/webhook` (handle `installation` /
  `installation_repositories` events; verify the webhook HMAC like `slack_events` does).
- **Per-tenant install store:** persist each user's `installation_id` + granted repos (extend
  the durable run store). `AppAuth` already mints a per-installation token, so no auth rework.
- **"Connect GitHub" UX** in Studio: button → `github.com/apps/<slug>/installations/new` → back
  to `/github/setup`.
This slice is the difference between "my bot" and "a product anyone installs." Keep it behind
the first proof so multi-tenant plumbing never blocks the first real run.

---

## 4. Invariants (non-negotiable)

1. **No hardcoded secrets, ever.** All tokens via `resolve_secret("<provider>:<handle>")` →
   `FABRI_CRED_<PROVIDER>_<HANDLE>`. Redact tokens in logs/traces.
2. **SSRF guard on every outbound request** (Linear, GitHub, Slack). No new bare `urlopen`.
3. **Tool contract:** every tool/handler returns `{ok, result?, error?}`; a raised exception
   becomes `{ok: false, error: ...}`, never an unpaired tool_use.
4. **Fail closed.** No PR, no Slack post, no Linear comment unless the prior gate genuinely
   passed. A verifier's own output — not a zero exit code — is the source of truth.
5. **Idempotency.** Re-running the same ticket updates the existing PR/branch, doesn't spawn
   duplicates (`open_or_update_pr` already does this for PRs — match it for branches/comments).
6. **No faked artifacts.** If tests didn't run, say so and stop. The recorded outcome must
   match what actually happened on disk and on the remote.

---

## 5. Verification — the actual gate (this is the deliverable)

A "done" claim requires ALL of:

1. **Mock unit tests** for each connector (Linear/GitHub/Slack) — offline, in CI.
2. **Env-gated live smokes**, one per connector, behind `@pytest.mark.live` (skipped unless
   `FABRI_LIVE_TESTS=1` and the creds are present). Each hits the real API and asserts a real
   response field (Linear comment url, GitHub PR number, Slack message ts). These prove the
   thing the mock tests cannot.
3. **ONE recorded end-to-end live run** of `fabri repo run --from-linear …` against:
   - a real Linear issue in the user's workspace,
   - a **throwaway** GitHub repo the user owns (seeded with a failing test),
   - a real Slack test channel.
   Commit the artifacts under `benchmarks/results/repo-run-<date>/`: the run trace JSONL, the
   **PR url**, the **Slack permalink**, the **Linear comment url**, and the diff the crew
   produced. This recorded run is the proof the whole exercise exists to produce.

If the live E2E can't be produced (missing creds, API change), report exactly which gate
blocked and STOP — do not paper over it with a mock or a hand-written artifact.

---

## 6. Setup the human must provide (list this back before you start)

Creds come from the **already-built** helper — do not ask for raw tokens, point the user at it:

```bash
python scripts/setup_bots.py all     # Slack bot + GitHub App (bot) + Linear OAuth
source .env.fabri.local
python scripts/setup_bots.py doctor  # live-checks Slack + Linear
```

That writes (see `docs/repo-run-setup.md`):
- **GitHub App:** `FABRI_CRED_GITHUB_APP_ID`, `FABRI_CRED_GITHUB_INSTALLATION_ID`,
  `FABRI_CRED_GITHUB_PRIVATE_KEY` (path to the PEM in `secrets/`), plus
  `GITHUB_APP_CLIENT_ID/_SECRET/_WEBHOOK_SECRET`, and `FABRI_REPO=owner/name`.
- **Slack:** `FABRI_CRED_SLACK_DEFAULT` (+ `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
  `FABRI_SLACK_CHANNEL`).
- **Linear:** `FABRI_CRED_LINEAR_DEFAULT` (OAuth access token).

Also required from the human:
- A **throwaway GitHub repo** they own, with the App installed on it, seeded with a tiny
  **failing test** so the crew has real work.
- One real **Linear issue** describing that trivial fix.

Note: `setup_bots.py` creates a **private** App by default (fine for this single-tenant
proof). The `--public` path (anyone can install, Vercel-style) belongs to Slice 6, not here.

---

## 7. Explicitly OUT of scope (descope hard)

- GitLab/Bitbucket parity for the new flow (GitHub only).
- MCP-based GitHub path, MCP SSE transport.
- Vault/encrypted credential backends (env-var store only).
- Multi-ticket batching, scheduling.
- Any second engineering agency.
- **Multi-tenant install (Slice 6) and its Studio UX** — designed above but explicitly
  deferred until the single-tenant run is proven. Do not start it early.

Ship the single vertical slice (Slice 1–5), proven live, then stop and report before Slice 6.

---

## 8. Definition of done

- [ ] Linear connector (fetch/comment) + GitHub branch+PR-from-checkout + Slack notify step,
      each with mock tests AND a passing env-gated live smoke.
- [ ] `fabri repo run --from-linear` stitches them with fail-closed named gates.
- [ ] One engineering agency operates on a real checkout and its test result gates the PR.
- [ ] Committed `benchmarks/results/repo-run-<date>/` with trace + real PR/Slack/Linear urls
      + the produced diff.
- [ ] `uv run pytest` green (offline); live smokes pass with `FABRI_LIVE_TESTS=1`.
- [ ] A short `docs/repo-run.md` quickstart: creds → one command → what you get.
