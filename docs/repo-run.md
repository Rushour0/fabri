# `fabri repo run` quickstart

`fabri repo run` takes a Linear issue, clones a GitHub repository, and runs a
Fabri agency crew against the checkout to make the requested change. Fabri then
verifies the result with its own test subprocess and, only when that verification
genuinely passes, pushes a branch, opens or updates a pull request, comments the
PR URL on Linear, and posts to Slack. It is end-to-end ticket-to-PR automation
with a fail-closed boundary: an unsuccessful gate stops every downstream action.

## 1. Configure credentials

Fabri resolves connector credentials with
`resolve_secret("<provider>:<handle>")`. The environment-backed credential store
maps each reference to `FABRI_CRED_<PROVIDER>_<HANDLE>`: both components are
uppercased, and every character outside `A-Z`, `a-z`, and `0-9` becomes `_`.
For example, `slack:default` maps to `FABRI_CRED_SLACK_DEFAULT`.

Repo-run needs these credential references:

| Service | Credential reference | Environment variable |
| --- | --- | --- |
| Linear | `linear:default` | `FABRI_CRED_LINEAR_DEFAULT` |
| GitHub PAT | `github:default` | `FABRI_CRED_GITHUB_DEFAULT` |
| Slack | `slack:default` | `FABRI_CRED_SLACK_DEFAULT` |

If an existing secret manager exposes values as shell variables, map them
without putting literal tokens in a command, YAML file, or repository:

```bash
export FABRI_CRED_LINEAR_DEFAULT="${LINEAR_OAUTH_TOKEN:?}"
export FABRI_CRED_GITHUB_DEFAULT="${GITHUB_PAT:?}"
export FABRI_CRED_SLACK_DEFAULT="${SLACK_BOT_TOKEN:?}"
export FABRI_SLACK_CHANNEL="${SLACK_CHANNEL_ID:?}"
```

`FABRI_SLACK_CHANNEL` selects the destination channel. As an alternative, set
`routing.slack.default_channel` in the agency YAML.

### Bootstrap the bots

The repository includes an interactive bootstrap helper:

```bash
python scripts/setup_bots.py all
source .env.fabri.local
python scripts/setup_bots.py doctor
```

`all` walks through GitHub, Slack, and Linear in that order. You can run one
provider at a time with `github`, `slack`, or `linear` in place of `all`.
`doctor` prints masked values and live-checks the configured Slack and Linear
credentials; GitHub App token creation is checked by repo-run itself.

The helper uses browser-based provider setup and OAuth callbacks, then writes
only to gitignored local paths:

- GitHub App: `FABRI_CRED_GITHUB_APP_ID`,
  `FABRI_CRED_GITHUB_INSTALLATION_ID`, and
  `FABRI_CRED_GITHUB_PRIVATE_KEY`, whose value is the path to
  `secrets/github-app.pem`. It also writes `GITHUB_APP_CLIENT_ID`,
  `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_WEBHOOK_SECRET`, and, when supplied,
  `FABRI_REPO`.
- Slack: `FABRI_CRED_SLACK_DEFAULT`, `SLACK_BOT_TOKEN`,
  `SLACK_SIGNING_SECRET`, and `FABRI_SLACK_CHANNEL`.
- Linear: `FABRI_CRED_LINEAR_DEFAULT`.

The merged environment is stored in `.env.fabri.local`; the env file and GitHub
PEM are permissioned `0600`. The helper refuses to write either secret path
unless Git reports that it is ignored. It provisions GitHub App credentials,
not a PAT; export `FABRI_CRED_GITHUB_DEFAULT` yourself when using PAT auth.

GitHub App auth requires the repo-specific extra:

```bash
pip install "fabri[repo-github]"
```

That extra installs `PyJWT[crypto]`. Repo-run selects GitHub App auth when
`FABRI_CRED_GITHUB_APP_ID` is set; otherwise it falls back to the PAT in
`FABRI_CRED_GITHUB_DEFAULT`.

Never hardcode credentials. Repo-run redacts known tokens, encoded token forms,
credential-bearing URLs, and common secret syntax from gate data, logs, traces,
errors, results, and produced diffs. Its Linear, GitHub API, and Slack HTTP
connectors validate the destination before opening it and revalidate every
redirect with `ValidatingRedirect`.

## 2. Run one issue

Run the command from the workspace where you want Fabri to keep its checkout,
trace, and results:

```bash
fabri repo run --from-linear <ISSUE-ID> --repo <owner/name> [--base main] [--config <agency.yaml>] [--test-cmd "<cmd>"] [--setup-cmd "<cmd>"]
```

| Flag | Meaning |
| --- | --- |
| `--from-linear <ISSUE-ID>` | Required. The Linear issue ID or identifier to work, such as `ENG-123`. |
| `--repo <owner/name>` | Required. The GitHub repository to clone and open a PR against. The current clone URL is unauthenticated, so private repositories also need ambient Git credentials. |
| `--base main` | Base branch; defaults to `main`. |
| `--config <agency.yaml>` | Agency entry YAML that defines the crew doing the work. The CLI displays this flag as optional, but the current runner requires a real config path, so supply it. An agency package directory is also accepted. |
| `--test-cmd "<cmd>"` | Authoritative verification command. It overrides every other test-command source. |
| `--setup-cmd "<cmd>"` | Optional command run inside the clone before the crew starts. The v1 default is no setup command. |

The test command resolves in this order:

1. `--test-cmd`
2. `FABRI_REPO_TEST_CMD`
3. `[agency].test_cmd` in the `agency.toml` beside the agency YAML, or inside
   the supplied agency directory
4. `python -m pytest -q`

Before execution, Fabri ensures recognized pytest commands include
`-p no:cacheprovider`; the source default is therefore effectively
`python -m pytest -q -p no:cacheprovider`.

There is no implicit setup step in v1. The first live target must be
dependency-free, or the command must include an explicit setup such as
`--setup-cmd "python -m pip install -e ."`. Setup and test strings are parsed
into argument lists and run without a shell, so operators such as `&&`, pipes,
redirections, and shell variable expansion are not interpreted.

## 3. The ten fail-closed gates

Each gate must pass before the next begins. A failure stops the sequence
immediately; an already completed upstream side effect is not rolled back.

1. **`resolve_creds`** — Resolve the Linear credential and select the GitHub
   authentication provider. Slack also uses `slack:default`, although the
   current notifier resolves that credential when gate 10 executes.
2. **`fetch_issue`** — Fetch and validate the Linear issue's identifier, title,
   description, and URL, then build the task text for the crew.
3. **`clone`** — Clone the target GitHub repository to a stable checkout
   directory. A retry reuses the directory, fetches origin, resets it to the
   requested base, and removes leftover untracked files.
4. **`setup`** — Run `--setup-cmd` in the clone when provided. When it is unset
   or blank, record a successful skipped gate.
5. **`agency_run`** — Materialize and run the crew as
   `fabri --config <agency.yaml> run "<issue text>" --session-id <sid>` in a
   subprocess rooted at the clone. The crew may edit any files in that
   checkout. Its prose output is advisory only.
6. **`verified_tests`** — Run the resolved test command independently in the
   clone. Fabri captures this subprocess's own return code, standard output,
   and standard error. That captured return code is the authoritative
   pass/fail signal; crew claims are never accepted as evidence.
7. **`branch_push`** — If changes exist, capture the produced diff, commit all
   tracked and untracked changes, and push `fabri/<issue-identifier>`. The
   credential-bearing push URL is passed only to Git and is not saved in
   `.git/config`.
8. **`open_pr`** — Open a draft PR, or update the existing open PR found through
   its stable Fabri marker.
9. **`comment_linear`** — Put the PR URL back on the Linear issue and capture
   the returned comment URL.
10. **`notify_slack`** — Post the PR notification to the configured Slack
    channel and require a normalized `{ok: true, result: ...}` response.

Gates 7 through 10 are unreachable unless gate 6, `verified_tests`, genuinely
passes. If verification fails, Fabri performs **no branch push, opens no PR,
creates no Linear comment, and posts no Slack message**.

## 4. Verification is the trust boundary

The agency subprocess is untrusted. A crew can say that tests passed, quote a
test command, or produce convincing prose; none of those claims authorize a
push or PR. Even after a successful agency subprocess, Fabri reruns the
authoritative command itself in the checkout.

For pytest commands, Fabri disables the cache provider with
`-p no:cacheprovider`, captures its own return code and output, and records that
evidence in the gate trace. Only a captured zero return code passes. If tests
do not run or do not pass, the run records the failure and stops instead of
fabricating an external artifact.

Both `--setup-cmd` and `--test-cmd` execute code from the cloned repository
directly on the local host; v1 does not place them in a container. This is
acceptable only for the single-tenant, human-gated v1 trust model. Run repo-run
against repositories and commands you trust.

## 5. Results bundle

Once the gated flow starts, both successful and failed runs write truthful,
redacted evidence under the UTC-dated directory:

```text
benchmarks/results/repo-run-<date>/
```

The bundle contains:

| File | Contents |
| --- | --- |
| `trace.jsonl` | The source trace plus a JSONL record for every attempted gate. |
| `result.json` | Overall status, attempted gates, side-effect URLs or IDs, and bundle metadata. |
| `trace_path.txt` | Path to the bundled trace. |
| `pr_url.txt` | PR URL, or an empty file if no PR was opened. |
| `linear_comment_url.txt` | Linear comment URL, or empty when unavailable. |
| `slack.json` | Slack message `ts`, channel, and a permalink when the notifier returns one. |
| `diff.patch` | The diff captured after verification passes and before the branch is pushed; it is empty if that point was never reached. |

Do not treat the existence of a bundle or an empty placeholder as proof of
success; `result.json` and the attempted gate records are the source of truth.
Tokens are redacted again at the bundle boundary, including from the trace and
diff.

## 6. Idempotent retries

The idempotency contract is keyed by the repository and Linear issue
identifier. Re-running the same ticket targets the same stable checkout and
`fabri/<identifier>` branch, updates the existing marked PR, and updates or
reuses the Linear comment instead of creating duplicates.

The current source fully implements stable branch reuse and open-PR updates.
Its Linear path only skips comment creation when the fetched issue text already
contains the PR URL; it does not yet expose an update-existing-comment
operation. Until that path is completed, an ordinary retry can create another
Linear comment.
