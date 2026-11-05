# Connections

A connection is a stored credential for one outside service. It is named `provider:handle`, for example
`cakemail:acct1` or `slack:T012345`. The provider is a connector from the catalog (see
[adding-tools.md](adding-tools.md)). The handle is a label you pick (it defaults to `main`) or, for Slack and Linear,
the team or workspace id.

Every connection has a scope kind. `org` connections belong to the whole install and are what this release makes.
`user` connections belong to one signed-in person and come with the server-side connect routes in a later release.

A setup refers to a connection by name only. `fabri setups tools add connector cakemail --workspace acct1` writes the
secret ref `cakemail:acct1`, and fabri looks the token up when a call is made. The token is not written to the setup
file, the agent's prompt or a log line, and the model's tools cannot read it: the run process holds the tokens its own
sources declare, but every tool subprocess it starts (`bash`, `python_exec`, MCP servers, sub-agents) gets an
environment with those tokens removed, except the ones an MCP server or sub-agent declares for itself. `kind: python` sources are the exception. They run inside the run process and
can read every token it holds, so only declare python callables you trust. See
[What a run can see](#what-a-run-can-see) below.

## Connect a service

```sh
printf '%s\n' "$TOKEN" | fabri connect cakemail --handle acct1 --token-stdin
fabri connect cakemail --handle acct1          # asks for the token with no echo (terminal only)
fabri connect cakemail --login                 # Cakemail only: log in once, fabri makes a token for you
```

On success it prints `connected cakemail:acct1` (or `replaced cakemail:acct1` when a row already existed) and the next
step:

```sh
fabri setups tools add connector cakemail --workspace acct1 --scope org:acme
```

Flags:

| Flag | Meaning |
|---|---|
| `--handle H` | The label. Letters, digits, `_`, `.`, `-`, up to 80 characters, starting with a letter or digit. Default `main`. A bad handle exits 2. |
| `--scope org\|user` | Default `org`. `user` exits 2 in this release: user connections are made through the server. |
| `--token-stdin` | Read the token from the first line of stdin. Without it a terminal gets a hidden prompt, and a non-terminal stdin exits 2. |
| `--login` | Cakemail only. Needs a terminal. Asks for your username and password, creates a personal access token, stores it. |
| `--no-verify` | Skip the check call. Without this flag fabri calls the provider's identity endpoint (Cakemail: `GET /users/self`) and stores nothing if the token is refused. |
| `--expires-days N` | With `--login`: lifetime of the new token. Default 365. |
| `--account-id ID` | With `--login`, repeatable: limit the new token to these Cakemail account ids. |
| `--home-root DIR` | Where `installs.db` lives. See below. |

There is no `--token VALUE` flag on purpose. A token on the command line lands in shell history and `ps`.

### Cakemail `--login`

`--login` sends your username and password to `https://api.cakemail.dev/token`, then calls
`POST /users/self/pats` to create a personal access token named `fabri <hostname> <date>`. The password and the
short-lived login token are kept in memory only and dropped as soon as the personal access token comes back. Only the
personal access token is stored.

The new token carries the 14 scopes that the 28 curated tools need, read from the pinned spec:
`campaign_blueprints:read`, `campaigns:read`, `campaigns:send`, `campaigns:send-test`, `campaigns:write`,
`contacts:read`, `contacts:write`, `lists:read`, `logs:read`, `reports:read`, `segments:read`, `senders:read`,
`tags:read`, `templates:read`. If you would rather make the token yourself, give it those scopes and use
`--token-stdin`.

## List and remove

```sh
fabri connections list [--provider P] [--json] [--home-root DIR]
fabri connections remove cakemail acct1
```

`list` shows `PROVIDER HANDLE SCOPE NAME STATUS EXPIRES`. `--json` prints the same rows. Neither shows a token. `remove`
exits 1 when no such connection exists. It deletes the stored copy only. The token stays valid at the vendor until you
revoke it there.

`fabri installs list|remove` still works for one release and reads the same file. It is an alias; use
`fabri connections` in new scripts.

## Where it is stored

Connections live in `installs.db`, the same SQLite file as the Slack, GitHub and Linear installs. fabri finds it at
`--home-root/installs.db`, else `$FABRI_INSTALL_DB`, else `$FABRI_HOME/serve/installs.db`.

**Tokens are stored in plain text in that file. fabri sets it, and its `-wal` and `-shm` side files, to mode 0600
(owner only) and warns once if the permissions are wider. It does not encrypt the file.** Encryption at rest is a later
item. Protect the file the way you protect any key file: keep the disk private and do not put the file in a backup or a
repo that others can read.

On the first start of a fabri with connections, the old `installs` (Slack) and `linear_installs` tables are copied into
the new `connections` table and then dropped. Views with the old names and columns stay for one release, so an older
fabri that opens the file can still read it. Nothing is migrated twice.

## How a run gets its token

- **`fabri run` and Python hosts** read the token from the connection store directly, or from the environment variable
  `FABRI_CRED_<PROVIDER>_<HANDLE>` when there is no stored row. This applies to every provider. With neither, the call
  fails with `no <provider> connection for this handle; run fabri connect <provider> or set FABRI_CRED_...`. The
  message names no handle and no token.
- **`fabri serve` and `fabri studio`** do not give a run the database. `FABRI_INSTALL_DB` stays on the launcher's deny
  list, so a run cannot open the file. Instead, when the service binds a run, it looks at the secret refs that the
  run's own `tools.sources` declare, keeps the ones its `credential_grants` allow, resolves just those, and puts each in
  that run's child environment as `FABRI_CRED_<PROVIDER>_<HANDLE>`. A run that uses `cakemail:acct1` receives that one
  variable. It gets no other Cakemail handle, no Slack bot token, no Linear token. Every other token stays inside the
  service process.
- `FABRI_RUN_ENV_ALLOW` still exists. It is the operator's blunt way to pass a whole variable family to every run
  (`FABRI_RUN_ENV_ALLOW=FABRI_CRED_*`). Prefer the scoped passing above.
- Overrides sent with `POST /runs` or `/fleets` cannot name a credential or a connection. Keys such as `secret_ref`,
  `credential_grants`, `header_source` and anything ending in `_ref` are refused.

### What a run can see

The tokens a run receives sit in the run process's environment. The `http` and `openapi` source tools read them there
and send them in the request, inside the run process. A subprocess the run starts gets only what it declared:

- Every manifest tool (`bash`, `python_exec`, `slack_post` and any tool in a `manifest_dir`) starts with every
  `FABRI_CRED_*` variable removed. A manifest tool that used to read a credential from the environment, such as the
  `slack_post`, `webhook_post` and `send_email` recipes, finds nothing there; use an `http` source for those calls.
- A stdio MCP server with `env_mode: inherit` (the default) keeps only the `FABRI_CRED_*` names its own declaration
  references through `env` or a `secret_ref`; with `env_mode: allowlist` it inherits none. Either way the values its
  `env` names are resolved by the run and passed under the names you chose.
- A sub-agent keeps only the refs its own config declares and its `credential_grants` allow.
- A `verify` command gets no `FABRI_CRED_*` variable.

`kind: python` sources run in the run process, so they can read every token the run holds. Only declare python
callables you trust. This is a mitigation, not isolation: revoking or rotating a connection does not reach a run that
has already started. A token broker, where the service makes the call on the run's behalf and the token never enters
the run, is planned for a later release.

## When a token stops working

If the provider answers 401 to a call that used a stored credential, the tool error says
`the stored <provider> credential was refused, run fabri connect <provider>`. It names the provider and never the
token. Run the command again with a fresh token; the new one replaces the old row whole, refresh token and expiry
included.

Slack and Linear tokens made through the server install flow keep working as before. Linear access tokens are renewed
by the service when they are within 5 minutes of expiry.
