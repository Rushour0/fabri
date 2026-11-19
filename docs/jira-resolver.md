# Jira ticket resolver

Label a Jira issue `fabri:fix` and fabri tries to turn it into a draft pull request. A poller
finds the labelled issue, checks that the issue's assignee added the label, and runs
`fabri repo run --from-jira <KEY>`. That run clones the routed repository, lets a crew make the
change, runs the repository's own test command, and only when that command exits zero pushes a
branch and opens a draft PR. The PR link, or a short failure summary, goes back on the issue as a
comment.

There is no webhook. Jira admin webhooks need a site admin, so the resolver polls with JQL instead.

## Setup, in order

### 1. Create a Jira API token

1. Sign in as the account the bot should act as and open
   <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Choose "Create API token", name it (for example `fabri resolver`), copy the value once.

Comments posted by the resolver appear under this account. The account needs to browse the
project and add comments; it does not need admin rights.

### 2. Export the Jira and model credentials

Keep the values in your shell or secret manager, never in a file in a repository:

```bash
export JIRA_URL="https://callplaybook.atlassian.net"
export JIRA_EMAIL="you@example.com"            # the account that owns the token
export JIRA_API_TOKEN="${JIRA_TOKEN_FROM_YOUR_SECRET_STORE:?}"
export ANTHROPIC_API_KEY="${ANTHROPIC_KEY_FROM_YOUR_SECRET_STORE:?}"
```

Requests use HTTP Basic auth built from `JIRA_EMAIL` and `JIRA_API_TOKEN` through the Jira
connector's header spec.

### 3. Give fabri a GitHub token that can push to play-shaper

Use a fine-grained personal access token limited to `CharlesLadera/play-shaper` with
"Contents: read and write" and "Pull requests: read and write", or the GitHub App described in
[repo-run.md](repo-run.md). For a PAT:

```bash
export FABRI_CRED_GITHUB_DEFAULT="${GITHUB_PAT_FROM_YOUR_SECRET_STORE:?}"
```

The token is passed to `git push` on the command line only. It is not written to the clone's
`.git/config`, and the crew subprocess does not see it.

### 4. Write the routing config

The resolver reads `$FABRI_HOME/jira-resolver.toml` (`FABRI_HOME` defaults to `~/.fabri`).
Start from the example in this repository:

```bash
mkdir -p ~/.fabri
cp examples/jira-resolver.example.toml ~/.fabri/jira-resolver.toml
```

```toml
label = "fabri:fix"
max_cost_usd = 10.0                          # model spend cap per run, in USD

[projects.AI]
repo = "CharlesLadera/play-shaper"
base = "main"
setup_cmd = "npm --prefix web ci"
test_cmd = "npm --prefix web test"
```

- `agency` is optional. Left out, it uses the `jira-fix-crew` agency bundled with fabri. Set it
  to use your own crew; a relative path resolves against the directory that holds the TOML file.
- `test_cmd` is the verification gate. It is run by fabri itself, not by the crew, and only its
  exit code counts. For play-shaper it is the same `npm test` (vitest) step that the repo's
  PR-checks workflow runs. Tests that need Supabase or Caylex skip when those are not reachable,
  so it runs on a laptop with no services.
- `setup_cmd` installs play-shaper's dependencies (`npm ci` in `web/`). It needs the npm
  registry and Node 24, the version CI uses.
- `acceptance_field` (optional, per project) names a Jira custom field such as
  `customfield_10050` that holds acceptance criteria. Without it the crew reads them from the
  description.
- More projects are more `[projects.KEY]` tables. `PBSCR -> marchsl/nycbbl` is planned and sits
  commented out in the example.

### 5. Try one issue, then start the poller

```bash
fabri jira poll --dry-run          # prints would_start records, claims and runs nothing
fabri jira poll                    # one pass
fabri jira poll --every 60         # keep polling every 60 seconds
```

Each pass runs, per configured project:

```text
project = "AI" AND labels = "fabri:fix" AND statusCategory != Done ORDER BY created ASC
```

To run one issue by hand without the poller:

```bash
fabri repo run --from-jira AI-123
```

Flags `--repo`, `--base`, `--test-cmd`, `--setup-cmd` and `--config` override the route.

## When a run starts

- The issue matches the JQL above.
- The most recent time the label was added (from the issue changelog) was by the issue's current
  assignee: the change author's `accountId` equals the assignee's. A label set when the issue
  was created counts as added by the issue's creator. Anyone else's label is skipped and logged.
- That label add has not run before. State lives in `$FABRI_HOME/jira/poll.db`, keyed by issue and
  changelog entry, so a run is claimed once per label add and survives restarts. Remove the label
  and add it again to rerun.

A skipped issue is logged on every pass until the assignee adds the label or it is removed.
Runs go one at a time. A run that crashes is recorded as failed and the poll carries on.

## What the bot will do

- Clone the routed repo under `$FABRI_HOME/jira/runs`, run `setup_cmd`, and run the
  `jira-fix-crew` agency (Claude Sonnet implements, Claude Haiku scouts) with the issue summary,
  description and acceptance criteria as its task. Spend stops at `max_cost_usd`.
- Run `test_cmd` itself and push only if it exits zero.
- Push a branch named `fabri/ai-123-<slug-of-summary>` and open a draft PR against `base`.
- Comment once on the issue: the PR link, or which gate failed and why. That comment goes through
  the Jira connector's `add_comment` tool, which always needs approval; the poller grants exactly
  one comment on that one issue and refuses any other tool call or issue.
- Leave a results bundle (trace, result, diff) under the run workspace's
  `benchmarks/results/repo-run-<date>/`.

## What the bot will not do

- Push to the default branch or to `base`.
- Force-push. If the branch already exists on GitHub (from an earlier run), fabri pushes to
  `<branch>-2`, `<branch>-3` and so on, and opens a new draft PR. Old branches are left as they are.
- Merge anything, mark a PR ready for review, or approve it.
- Open a PR when the tests fail, did not run, or the crew changed nothing.
- Transition, edit, assign or create Jira issues.
- Post to Slack or comment on Linear.
- Run on issues in projects that have no route.

## Limits worth knowing

- `setup_cmd` and `test_cmd` run code from the cloned repo on the host, and the crew has a bash
  tool scoped by working directory only. This is not a security sandbox. Run the resolver only
  against repositories you trust, on a machine without production credentials in its environment.
- Ticket text is untrusted input to the crew. The fail-closed test gate and the draft-only PR are
  what keep a bad change from landing; a human still reviews every PR.
- The changelog check trusts Jira's record of who added the label. An assignee change after the
  label was added makes the label no longer count until the new assignee adds it.
