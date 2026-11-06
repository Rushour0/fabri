# Connections

A connection is a stored credential for one outside service. It is named `provider:handle`, for example
`cakemail:acct1` or `slack:T012345`. The provider is a connector from the catalog (see
[adding-tools.md](adding-tools.md)). The handle is a label you pick (it defaults to `main`) or, for Slack and Linear,
the team or workspace id.

Every connection has a scope kind. `org` connections belong to the whole install. `user` connections belong to one
person: the handle is that person's user id. Under `fabri serve` with auth on they are made through
`GET /connect/<provider>?scope=user` or `POST /connections` with `"scope": "user"` (see
[Connect through the server](#connect-through-the-server)). On the command line, `fabri connect <provider> --scope user`
works for connectors that use loopback OAuth, such as Gmail.

A setup refers to a connection by name only. `fabri setups tools add connector cakemail --workspace acct1` writes the
secret ref `cakemail:acct1`, and fabri looks the token up when a call is made. The token is not written to the setup
file, the agent's prompt or a log line. Under `fabri serve` a run never holds a stored token at all: a credential
broker in the service process makes the credentialed calls for it. See
[Approving calls under fabri serve](#approving-calls-under-fabri-serve) and [What a run can see](#what-a-run-can-see).

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
| `--scope org\|user` | Default `org`. `user` works for loopback OAuth connectors such as `gmail`, and `--handle` is then the user id the connection belongs to. For every other connector `user` exits 2 and points at `GET /connect/<provider>` on `fabri serve`. |
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

## Connect through the server

`fabri serve` and `fabri studio` serve these routes:

| Route | What it does |
|---|---|
| `GET /connections` | The rows, without tokens. A non-admin sees org rows and their own user rows. |
| `POST /connections` | Body `{provider, handle?, token, scope?, verify?}`. Stores a pasted token. |
| `DELETE /connections?provider=P&handle=H` | Removes one row. Answers `{"deleted": true\|false}`. |
| `GET /connect/<provider>?scope=org\|user` | Starts the OAuth flow: a 302 to the vendor's sign-in page. |
| `GET /connect/<provider>/callback` | Where the vendor sends the browser back. Stores the row. |
| `GET /slack/install`, `/linear/install` and their `/oauth/callback` paths | The older install paths, now aliases of the two above. |

With auth on, an org change needs an admin, and a user connection belongs to the signed-in user who made it. The
callback completes only for the person who started the flow. A callback that reaches anyone else is refused before the
code is exchanged, and the pending flow stays for its owner.

### Cross-site requests

Any web page you open can make your browser send a request to a fabri on `localhost`. So every connection change
(`POST` and `DELETE /connections`, starting `/connect/<provider>`, and the older install paths) is checked before
anything else, whether auth is on or off:

- `Sec-Fetch-Site: cross-site` is refused with 403.
- An `Origin` header that is not the service's own origin is refused with 403. The own origins are the origin of
  `FABRI_PUBLIC_BASE_URL` and, when the `Host` header names a loopback address, `http://<Host>` and `https://<Host>`.
  `Origin: null` is refused too.
- `POST /connections` needs `Content-Type: application/json`. Anything else gets 415, so a page cannot send the body
  as a plain form or `text/plain`.

With auth off there is no signed-in user to check, so a change must also show that it comes from the service's own
pages, or carry the service or admin token. It counts as same-origin when it has a matching `Origin`, or
`Sec-Fetch-Site: same-origin` with a `Host` that is a loopback address or the public base URL's host (this stops a
DNS-rebound page). To start a flow, `Sec-Fetch-Site: none` (a URL typed into the address bar) also counts. Studio's own
links and requests pass. A script such as `curl` sends none of these headers, so with auth off it needs
`Authorization: Bearer $FABRI_ADMIN_TOKEN` (or the service token when no admin token is set), or an `Origin` header
with the service's own origin. A same-site page on another port of `localhost` is not same-origin and is refused.

The callbacks are not checked this way. They are top-level navigations from the vendor's site, and the pending flow
already ties each one to the person who started it. Callback responses carry `Referrer-Policy: no-referrer` and
`Cache-Control: no-store`, and the service's request log holds the method and path only, never the query string with
the code and state. A reverse proxy in front of fabri keeps its own access log: set it to drop query strings for
`/connect/*/callback`, `/slack/oauth/callback` and `/linear/oauth/callback`.

### Hosted deploys need auth

`fabri serve` and `fabri studio` check `FABRI_PUBLIC_BASE_URL` at start:

- It must be an `http` or `https` URL with no user, query or fragment, and it must use `https` unless its host is a
  loopback address (`localhost`, `127.0.0.1`, `::1`).
- When it names a hosted address (anything that is not loopback) and auth is off, the service refuses to start:
  `fabri serve refused to start: FABRI_PUBLIC_BASE_URL names a hosted address but auth is off, ...`. Turn auth on in the
  service config, or use a loopback base URL for local work. A bearer token alone does not lift this, because a browser
  that starts a connect flow cannot send one.

Leaving `FABRI_PUBLIC_BASE_URL` unset is allowed. The OAuth routes then answer that OAuth is not configured.

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
  message names no handle and no token. Nothing sits between the code and the token here, because the process that
  makes the call already holds it. On a terminal, `fabri run` asks `[y/N]` before an `always_approve` call.
- **`fabri serve` and `fabri studio`** run each agent as a child process that does not get the database
  (`FABRI_INSTALL_DB` stays on the launcher's deny list) and does not get a token either. The service holds one
  credential broker. When it binds a run, it reads the run's own `tools.sources`, and if any source declares a
  `secret_ref` it opens a Unix socket for that run (mode 0600, in a 0700 directory) and gives the run two variables:
  `FABRI_CRED_BROKER_SOCKET` (the socket path) and `FABRI_CRED_BROKER_KEY` (a random 256-bit key). A run whose sources
  declare no `secret_ref` gets neither. Every credentialed `http` or `openapi` source tool, and every `http` MCP
  source with a `secret_ref` header, sends its request to the broker instead of calling the provider itself. The
  broker checks the request, resolves the token from the connection store on that call, adds it to the outbound
  request, and sends it. The reply passes through with the token value scrubbed out. Because the token is looked up on
  each call, a revoked connection stops working on the next call and a refreshed one is used at once.
- A source whose only credentials are `env:` variables or literal headers is not brokered. Its values are in the run's
  environment because the operator put them there.
- `FABRI_RUN_ENV_ALLOW` still exists. It is the operator's blunt way to pass a whole variable family to every run
  (`FABRI_RUN_ENV_ALLOW=FABRI_CRED_*`). It puts those values in the run process, outside the broker.
- Overrides sent with `POST /runs` or `/fleets` cannot name a credential or a connection. Keys such as `secret_ref`,
  `credential_grants`, `header_source` and anything ending in `_ref` are refused.

### Approving calls under fabri serve

Only calls whose approval class is `always_approve` need a person. The broker works out the class itself from the
run's bound `run.yaml` (the tool's own class, raised by the policy floors and by the catalog floor for the request
host). Nothing in a request can lower it.

- `read` and `write` calls pass the broker with no question. They are still limited to the declared sources and tools.
- An `always_approve` call is held. The broker asks the run's questions inbox (Studio, or Slack for a run bound to
  Slack) and shows the rendered request: tool, provider, method, URL, the body with secrets removed, and the names and
  values of any extra headers. A person answers `approve` or `deny` through `POST /runs/<id>/answer`, and the reply
  must carry the matching `question_id` (see [setups-api.md](setups-api.md)).
- An approval covers that one request only. The broker hashes the exact request it prepared, sends that same object
  after the yes, and drops the approval after one use or `120` seconds, whichever is first. The same call asked again
  gets a new question.
- With no one to ask (a host that did not wire an approver), an `always_approve` call is refused with
  `approval_required`. With no answer inside `FABRI_APPROVAL_TIMEOUT_S` (default 300 seconds) it is refused with
  `approval_timeout`. A denial gives `approval_declined`. In every case the call is not made.
- For an `http` MCP source, the methods `initialize`, `ping`, `tools/list`, `resources/list`, `resources/read`,
  `prompts/list`, `prompts/get` and a few others count as `read`. `tools/call` gets the class of the remote tool
  (annotations, the source's `approval:` block, policy floors, the catalog minimum). A `tools/call` for a tool the
  broker has not seen in a `tools/list` reply, any other method, and a batch body are `always_approve`.

The broker writes one audit line per decision (source, tool, method, host, path, class, decision, status code, never a
value) to the service log, and to the file named by `FABRI_CRED_AUDIT_LOG` when that is set.

### What a run can see

Under `fabri serve` a run sees the socket path and the key, and nothing else about the token.

- The run process holds no stored token. Code in the run that reads its own environment finds no `FABRI_CRED_<PROVIDER>_`
  variable for a stored connection.
- Every tool subprocess (`bash`, `python_exec`, manifest tools, MCP servers, the `verify` command) starts with every
  `FABRI_CRED_*` variable removed, which includes the two broker variables. A manifest tool that used to read a credential from
  the environment, such as the `slack_post`, `webhook_post` and `send_email` recipes, finds nothing. Use an `http`
  source for those calls. A manifest `credentials:` list cannot hand out the broker variables.
- A sub-agent keeps the two broker variables and sends its credentialed calls to the same broker, which answers from
  the parent run's binding. It can use only the sources that were bound when the run started, which includes sources
  in the static `tools.agents[*].config` files the template names. A sub-agent config chosen later by the model cannot
  use a credentialed source: the call fails with `source <name> is not bound to this run`.
- The broker applies the header specs of the declared source. A request cannot choose its own auth header, add an
  `Authorization`, `Cookie` or `X-API-Key` header, or name a different host. The URL comes from the template the run
  declared, with path values filled in by the broker. It does not follow redirects.
- A run cannot reach another run's broker. The broker checks that the connecting process has the service's user id and,
  where the platform reports the peer's process id (Linux and macOS), that it is a descendant of this run's process.
  It also checks the key.

#### Runs that share the service's OS user (`FABRI_ALLOW_SHARED_UID`)

The broker keeps tokens out of the run process. It cannot keep them away from code that runs as the same OS user as
the service. Today `fabri serve` starts every run as a child process under its own user, so the run's `bash` tool, a
python source or anything the model gets the run to execute can open `installs.db` next to the run directory and
read every stored token, other tenants' included. On most Linux and macOS systems a same-user process can also read
the service's memory. File mode 0600 does not help, because the run is the owner.

So `fabri serve` refuses a run that needs a stored credential when that run would execute as the service's user.
`POST /runs` answers 400 and nothing of the run is kept. The error names the sources, the reason and the flag:
`sources ck use stored credentials, and this run would execute as the same OS user as fabri serve, so it could read
installs.db and the service's memory. ... For local development only, set FABRI_ALLOW_SHARED_UID=1 on the service`.

Set `FABRI_ALLOW_SHARED_UID=1` in the service environment to allow these runs. Do this only on a machine where you
own every stored token, such as your laptop. Any other value, including `true`, leaves the refusal on. Runs whose
sources use no stored credential (no auth, or `env:` variables) are not affected.

Real isolation needs runs to start under a separate OS user, or in a container that mounts only the run directory
and cannot see `installs.db` or the service's processes. That is the coming container launch mode. A launch mode
that isolates runs sets `FabriService.launch_isolation` to something other than `shared_uid` (the default comes
from `fabri.service.launcher.LAUNCH_ISOLATION`), and the refusal then stops applying without the flag.

The check covers brokered runs. The `FABRI_CRED_SNAPSHOT=1` opt-out below is not checked, and it puts the tokens in
the run's environment besides.

#### Sources that cannot be brokered

- A stdio MCP server (`command:`) that needs a stored credential is refused under `fabri serve`, because the
  token would have to be placed in the server's environment, which puts it in the run again. The run fails at bind
  with `a stdio MCP server cannot use a stored credential under fabri serve; use the http transport, or set
  FABRI_CRED_SNAPSHOT=1 on the service`.
- A `kind: python` source that declares a stored credential is refused the same way. The python callable would run
  inside the run process with the token. Use an `http` or `openapi` source, or the snapshot opt-out below.
- A `secret_ref` inside an MCP source's `env:` cannot be brokered. Put it in `headers` on an http MCP source.
- A source with no stored credential (`env:` variables or no auth) is unaffected, and python and stdio sources work
  as before.

#### `FABRI_CRED_SNAPSHOT=1`, the weaker opt-out

Set `FABRI_CRED_SNAPSHOT=1` in the service environment to turn the old behavior back on for every run. At launch the
service resolves each secret ref the run's sources declare (limited by its `credential_grants`) and puts it in the
run's environment as `FABRI_CRED_<PROVIDER>_<HANDLE>`. No broker is started. This is weaker in three ways:

- The token sits in the run process for the life of the run. Any code in that process can read it, including
  `kind: python` sources and anything the model gets the process to run.
- Revoking or rotating a connection does not reach a run that has already started.
- No broker means no rendered approval card, and the run-side approval gate is the only one.

Subprocesses still lose the variables as described above. Use the opt-out only for a crew that cannot move off a
stdio MCP server or python source that needs a stored credential, and prefer moving the crew.

#### What a library host gets

- A host that uses `FabriService`, in-process or over HTTP, gets the broker with nothing to set up. Runs are
  subprocesses, the service owns the tokens, and approval goes through the answer route or Slack.
- A host that calls `build_tools` or `run_agent` in its own process already holds the tokens in that process, so there
  is no boundary and nothing changes. Credentials resolve in-process and a Python `approver=` handles `always_approve`.
- A host that launches runs itself with `launch_run` can own a `CredentialBroker(install_db=...)`, call `bind(...)`,
  and pass `binding.env()` to the run. With no listener wired, every `always_approve` call is refused. That is the
  safe default.

#### Limits that remain

- Code in the run's own process tree can use the broker for the calls the run declared. The key is in the run
  process's environment, so a `kind: python` source with no credential, or anything the model makes the run process
  execute, can send a declared read or write. It cannot read the token, and it cannot send an `always_approve`
  call without a person saying yes. A subprocess the run starts loses the variables, but a process the run starts
  can still find the key if it can read the parent's environment, as same-user processes usually can on Linux and
  macOS.
- DNS rebinding. The broker checks that the host resolves to a public address right before it sends. The check and
  the send are two lookups. A declared host whose DNS an attacker controls could change its answer between them.
  Pinning the resolved address is a later item. Use hosts you trust.
- Tokens are stored in plain text in `installs.db` (see [Where it is stored](#where-it-is-stored)). The broker reads
  that file as the service user; anything else that can read the file reads the tokens. That includes every run
  started with `FABRI_ALLOW_SHARED_UID=1`, since those runs execute as the service user. See
  [Runs that share the service's OS user](#runs-that-share-the-services-os-user-fabri_allow_shared_uid).
- Env fallback. When a ref has no stored row, the broker reads `FABRI_CRED_<PROVIDER>_<HANDLE>` from the service's
  own environment. A stored row that exists but is revoked, expired or another user's refuses the call and does not
  fall back to the environment.
- `fabri run`, a Python host and the snapshot opt-out have no broker. Their code sees the token.

#### Environment reference

| Variable | Where | Meaning |
|---|---|---|
| `FABRI_CRED_BROKER_SOCKET` | run | Path of this run's broker socket. Set by the service, removed from every tool subprocess. |
| `FABRI_CRED_BROKER_KEY` | run | The run's 256-bit key. Set by the service, removed from every tool subprocess. Never logged. |
| `FABRI_ALLOW_SHARED_UID` | service | `1` allows runs that need a stored credential to execute as the service's own OS user. Local development only. Without it such runs are refused with 400. |
| `FABRI_CRED_SNAPSHOT` | service | `1` puts granted token values in each run's environment and starts no broker. The weaker opt-out. |
| `FABRI_APPROVAL_TIMEOUT_S` | service and run | Seconds to wait for a person to answer an approval question. Default 300. |
| `FABRI_CRED_AUDIT_LOG` | service | Path of a JSON-lines file for broker decisions and credential lookups. No values. |
| `FABRI_FETCH_ALLOW_PRIVATE` | service and run | Lets http and MCP sources call loopback and private addresses, for a local MCP server. Off by default. |

## When a token stops working

If the provider answers 401 to a call that used a stored credential, the tool error says
`the stored <provider> credential was refused, run fabri connect <provider>`. It names the provider and never the
token. Run the command again with a fresh token; the new one replaces the old row whole, refresh token and expiry
included.

Slack and Linear tokens made through the server install flow keep working as before. Linear access tokens, and any
other OAuth token that has a refresh token, are renewed by the service when they are within 5 minutes of expiry. The
caller that renews takes a short lease on the row, makes the vendor call with no database lock held, and stores the new
token only if the stored refresh token is still the one it spent. A second caller meanwhile uses the current token if it
is still valid, or waits up to 20 seconds for the renewal.
