# Repo-run bot setup (Slack + GitHub App + Linear)

Create the three **bots** a real fabri repo run needs, run their OAuth flows, and
wire the credentials into a **gitignored** `.env.fabri.local`. This repo is public,
so nothing secret is ever committable: creds land only in `.env.fabri.local`
(matched by `.env*`) and the GitHub App key in `secrets/` (both gitignored).

## TL;DR

```bash
python scripts/setup_bots.py all      # walks github → slack → linear
source .env.fabri.local
python scripts/setup_bots.py doctor   # confirms what's wired (values masked)
```

The helper automates the **OAuth token exchange + safe env wiring**. You still
create each app in its browser console (the script opens the right page and, for
GitHub, pre-fills everything via an app manifest).

## What you need first

- A **throwaway GitHub repo** you own, seeded with one small **failing test** (so
  the crew has real work). e.g. `yourname/fabri-run-sandbox`.
- A Slack workspace where you can install an app, and one **test channel** id.
- A Linear workspace (a test team is fine) and one **real issue** describing the
  trivial fix in the sandbox repo.

## Per-provider (minimal scopes)

| Provider | You do (browser) | Script does | Scopes |
|---|---|---|---|
| **GitHub App** | Click "Create" on the pre-filled manifest, then "Install" on the one repo | Captures the app id, private key (PEM), client id/secret, webhook secret, and installation id automatically via the manifest + setup-URL redirect | `contents:write`, `pull_requests:write`, `issues:write`, `metadata:read` |
| **Slack bot** | Create app *From a manifest* (script prints it), copy Client ID/Secret + Signing Secret | Runs OAuth v2, gets the `xoxb-` bot token | `chat:write`, `channels:read` |
| **Linear** | Create an OAuth application, set redirect URI to the localhost callback, copy Client ID/Secret | Runs OAuth (`actor=application` → bot identity), gets the access token | `read,write` |

Callback URL used by all flows: `http://localhost:8976/callback/<provider>` — add it
verbatim where each console asks for a redirect URI.

## What lands in `.env.fabri.local`

```
FABRI_CRED_GITHUB_APP_ID / _INSTALLATION_ID / _PRIVATE_KEY   # GitHub App (bot)
GITHUB_APP_CLIENT_ID / _CLIENT_SECRET / _WEBHOOK_SECRET      # for user-OAuth + webhooks later
FABRI_REPO=owner/name
FABRI_CRED_SLACK_DEFAULT  +  SLACK_BOT_TOKEN  +  SLACK_SIGNING_SECRET  +  FABRI_SLACK_CHANNEL
FABRI_CRED_LINEAR_DEFAULT
```

`FABRI_CRED_<PROVIDER>_<HANDLE>` is exactly what fabri's `resolve_secret("provider:handle")`
reads (`src/fabri/tools/credential_store.py`). The `SLACK_*` duplicates match the names
`slack_notify` / config already expect.

## Safety notes

- The script **refuses** to write to any path git does not ignore, and `chmod 600`s
  both the env file and the PEM.
- Values are **masked** in all output, including `doctor`.
- To rotate/revoke: delete the app in its console, `rm .env.fabri.local secrets/github-app.pem`,
  re-run. Nothing to un-commit because nothing was committable.

## Auth choices, briefly

- **GitHub App (not a PAT):** scoped per-repo, short-lived installation tokens, bot
  identity, and installable on *other* people's repos later. The repo-run harness
  mints an installation token from the App id + PEM at run time (RS256 JWT →
  `POST /app/installations/{id}/access_tokens`) — that's the one place crypto is
  needed, and it lives behind a `GitHubAuth` seam so PAT vs App is swappable.
- **Slack/Linear OAuth (not raw personal tokens):** revocable, scoped, and post as a
  bot rather than as you.

To make the App **public** (anyone can install it, Vercel-style) answer `y` at the
"make it PUBLIC?" prompt — but that's the multi-tenant path; keep it private for the first
single-tenant proof.

Next: hand **`docs/repo-run-brief.md`** to the implementer to build the connectors +
`fabri repo run --from-linear` on top of these creds. That brief's Slice 6 covers the
public "install anywhere" flow, deferred until the first real run is proven.
