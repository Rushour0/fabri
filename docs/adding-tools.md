# Adding tools to a setup

Three kinds of tool source can be added to any scope with one command:

- an OpenAPI 3 spec (a file or a URL)
- an MCP server (a stdio command or an HTTP URL)
- a connector (GitHub, Linear, Slack, Cakemail, Gmail; Composio is experimental)

Each command writes one plain YAML file, `<layer>/tools/sources/<name>.yaml`,
checks that it builds under the host's policy, lints the layer stack and
prints what was added: the tool count and the approval class of every tool.
The file has the same shape as `tools/tools.yaml`, so it is reviewed in git
like any other change and shows up in Fabri Studio's tool catalog on the next
resolve. Nothing restarts.

Credentials are never written. Every flag that takes a credential takes an
environment variable name (`--auth-env PETS_TOKEN`) or a credential store
reference (`--secret-ref pets:prod`, `--workspace acme`). A value that looks
like a token is refused.

## Where it writes

Pick the layer with `--scope` and say where scopes live with the same config
`fabri serve` uses:

```sh
export FABRI_SETUPS_CONFIG=data/setups.yaml   # or --config
export FABRI_SETUPS_ROOT=data                 # or --root, when the config's root is not right for you
fabri setups tools list --scope org:acme
```

Scopes are `baseline`, `pack:<name>`, `org:<org>` and `org:<org>/<project>`
(the long form `org:<org>/project:<project>` works too). The scope directory
must exist. The config's `policy` and `global_allow` apply, exactly as they do
for the API. `--layer DIR` writes into a directory instead, with a permissive
policy unless you pass `--global-allow FILE`.

`fabri setup` and `fabri setups` are the same command.

## OpenAPI

```sh
# A local spec. It is copied to tools/specs/pets.yaml so the layer stands alone.
fabri setups tools add openapi ./petstore.yaml --scope org:acme --name pets --auth-env PETS_TOKEN

# A URL. The YAML keeps the URL and fetches it on each resolve; --snapshot saves a copy instead.
fabri setups tools add openapi https://api.example.com/openapi.json --scope org:acme/q3 \
  --include 'invoice_*' 'customer_*' --exclude '*_admin_*' --secret-ref billing:prod

# Narrow by method, raise every tool to at least write, set the base URL.
fabri setups tools add openapi ./crm.yaml --scope org:acme --methods GET POST \
  --approval write --base-url https://crm.example.com

# Check first. Nothing is written.
fabri setups tools add openapi ./crm.yaml --scope org:acme --dry-run
```

Output:

```
added tools/sources/pets.yaml (openapi)
  7 tools: 4 read, 3 write, 0 always_approve
  APPROVAL  TOOL           ENDPOINT
  read      owner_list     GET /v1/owners
  read      pet_get        GET /pets/{petId}
  ...
```

The file it wrote:

```yaml
# Added with `fabri setups tools add openapi`. Plain YAML: edit, review and commit it like code.
# Credentials are env var names or secret refs only.
sources:
- kind: openapi
  name: pets
  spec: specs/pets.yaml
  headers:
    Authorization:
      env: PETS_TOKEN
      prefix: 'Bearer '
allow:
- pet_list
- pet_create
- pet_get
...
```

`allow` lists the tools by name, so an operation added to the spec later does
not appear until someone adds it (`fabri setups tools sync` shows it).
DELETE operations are left out unless you pass `--allow-delete`, and the
policy must allow DELETE too. Other flags: `--auth-header`, `--auth-prefix`
(default `Bearer `), `--header Name=ENV_VAR` (repeatable), `--strip-prefix`.

## MCP servers

```sh
# stdio: the command line runs when the setup resolves. --env NAME passes NAME through;
# --env CHILD=PARENT sets CHILD in the server from PARENT here.
fabri setups tools add mcp files --scope org:acme \
  --command "npx -y @modelcontextprotocol/server-filesystem /srv/shared"

fabri setups tools add mcp notion --scope org:acme \
  --command "npx -y @notionhq/notion-mcp-server" --env NOTION_TOKEN

# HTTP (MCP Streamable HTTP, JSON or SSE answers)
fabri setups tools add mcp tracker --scope org:acme --url https://mcp.tracker.example/mcp \
  --auth-env TRACKER_TOKEN --include 'list_*' 'get_*' 'create_issue'

# Write without starting the server (no token here, or the server is down)
fabri setups tools add mcp tracker --scope org:acme --url https://mcp.tracker.example/mcp \
  --auth-env TRACKER_TOKEN --no-connect
```

By default the command starts the server and lists its tools. Tools are named
`mcp_<name>_<tool>`. When the server was reached, `allow` names each tool and
the always_approve ones are written under `approval`; with `--no-connect`,
`allow` is `mcp_<name>_*` (or your `--include` patterns) and classes are
decided when the setup resolves. The older two-endpoint HTTP+SSE transport is
not supported. A relative path in `--command` is relative to wherever the
host process runs, so prefer absolute paths or commands on `PATH`.

The policy must list `mcp` in `allowed_kinds`. The strict policy does not:

```yaml
# global_allow file named in setups.yaml
allowed_kinds: [openapi, http, mcp]
```

## Connectors

```sh
fabri setups tools connectors                      # what is available

fabri setups tools add connector github --scope org:acme             # Docker, GITHUB_PERSONAL_ACCESS_TOKEN
fabri setups tools add connector github --scope org:acme --remote --token-env GH_TOKEN   # hosted server
fabri setups tools add connector linear --scope org:acme             # hosted MCP, LINEAR_API_KEY
fabri setups tools add connector slack --scope org:acme --workspace T0123   # secret ref slack:T0123
fabri setups tools add connector slack --scope org:acme --name ops_slack --token-env OPS_SLACK_TOKEN
fabri setups tools add connector cakemail --scope org:acme --workspace acct1   # 28 curated tools
fabri setups tools add connector cakemail --scope org:acme --token-env CAKEMAIL_TOKEN --all-tools
fabri setups tools add connector composio --scope org:acme --url https://example.test/mcp --include GMAIL_SEND_EMAIL
```

Three flags apply to some connectors only:

- `--url` is the server URL. Only `composio` takes it, and it needs it (https only).
- `--include` names the tools to expose. `composio` needs it, with exact tool names (no `*?[`). On
  other connectors it replaces the curated list.
- `--all-tools` widens a connector that has a curated list (`cakemail`) to every tool in its pinned spec. The
  approval floor still applies to each one. It cannot be combined with `--include`, and connectors without a
  curated list refuse it.

| Connector | Source kind | Default credential | What you get |
|---|---|---|---|
| `github` | mcp (Docker, or `--remote`) | `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub's official MCP server: issues, PRs, code, actions |
| `linear` | mcp (hosted) | `LINEAR_API_KEY` | Linear's MCP server: issues, projects, comments |
| `slack` | http | `SLACK_BOT_TOKEN` | `slack_channels_list`, `slack_channel_history` (read), `slack_post_message` (always_approve) |
| `cakemail` | openapi (pinned spec) | `CAKEMAIL_TOKEN` | 28 curated tools: lists, contacts, segments, tags, senders, templates, campaigns. 21 read, 7 always_approve. See [connectors/cakemail.md](connectors/cakemail.md) |
| `gmail` | http | `GMAIL_TOKEN` (usually `--workspace '${actor}'`, one connection per user) | `gmail_labels_list` (read), `gmail_messages_send` (always_approve). `--all-tools` adds `gmail_drafts_create` (always_approve, needs the `compose` scope) and two reads that need the `readonly` scope. See [connectors/gmail.md](connectors/gmail.md) |
| `composio` | mcp (experimental, written disabled) | `COMPOSIO_API_KEY` | Tools of the apps connected in your Composio account, every one always_approve. See [connectors/composio.md](connectors/composio.md) |

`--workspace` uses the credential store ref `<provider>:<workspace>`. fabri resolves it from a stored connection
(`fabri connect <provider> --handle <workspace>`, see [connections.md](connections.md)), or from
`FABRI_CRED_<PROVIDER>_<WORKSPACE>` when there is no stored row. Stored tokens are plain text in a 0600 file. Under
`fabri serve`, a run never receives a stored token. A credentialed `http`, `openapi` or `http` MCP source sends each
request to a credential broker in the service process, which adds the token, sends the call and returns the result with
the token removed. Only `always_approve` calls need a person's yes. A stdio MCP source or a `kind: python` source that
needs a stored credential is refused under `fabri serve`; use an http or openapi source, or set
`FABRI_CRED_SNAPSHOT=1` on the service (weaker, see [connections.md](connections.md#fabri_cred_snapshot1-the-weaker-opt-out)).
`fabri run` and Python hosts resolve the token in the same process. Details and limits are in
[connections.md](connections.md#what-a-run-can-see). To keep tenants apart, wrap the store a run gets in
`ScopedCredentialStore(store, ["slack:acme", "linear:*"])`: any other ref
fails with `CredentialScopeError`. Pass `audit_path=` (or set
`FABRI_CRED_AUDIT_LOG` for the default store) to append one JSON line per
lookup with the provider, handle, actor and result, never the value. To write a new connector, see
[adding-a-connector.md](adding-a-connector.md). Connectors do not reach the service when added; pass
`--connect` to list the tools of an MCP connector now. A missing token here is
a warning, not an error: the file is right and the tools build where the
token is set. The github and linear connectors need `mcp` in `allowed_kinds`.

### How connector approval floors work

Every catalog expansion writes `approval_mode: tighten` and `catalog: <id>` on each source. Those keys sit in
editable YAML, so they are not what holds a write behind approval. The floor is bound to the request itself:

- A connector module in `src/fabri/tools/connector_catalog/` may set `HOSTS`, a tuple of lower-case host names it
  owns (Slack sets `("slack.com",)`). A host matches itself and its subdomains.
- It may also set `READS`, a tuple of `(METHOD, path regex)` pairs trusted as read. The regex must match the whole
  URL path. Slack lists `GET /api/conversations.list` and `GET /api/conversations.history`.
- For `http` and `openapi` tools, `request_floor(url, method)` looks up the owner of the request host. A call to an
  owned host is `always_approve` unless its method and path are in `READS`. The tool's name, the method the YAML
  claims and the presence of the two keys do not matter. Editing the YAML or the pinned spec copy cannot lower it.
- Two modules may not claim the same host, and every module is checked when the package loads (`HOSTS` and `READS`
  are optional; a module with neither has no host floor).
- MCP tools have no URL per tool, so their floor stays tool-name based through `minimum(tool_name, method)` and the
  `catalog` key. An http MCP server is matched by the host of its server URL; a stdio server only by its `catalog` key.

A hand-written source on a host no module owns, and without `approval_mode` or `catalog`, can still lower a class,
and lint warns when it does. Adding a connector's hosts is a change to its module, reviewed as code. Nothing else
needs editing. The functions live in `fabri.tools.connector_catalog`
(`catalog_for_url`, `request_floor`). The `fabri.tools.connectors` facade re-exports both.

A manifest tool sees no `FABRI_CRED_*` variable unless it lists `provider:handle` patterns (handle may be `*`) in an optional `credentials` field, and then only the matching variables are passed to its process. Under `fabri serve` a run holds no stored token (the broker keeps them), so there is nothing to match; use an `http` source for credentialed calls. The broker's own variables are never handed out this way.

## Approval classes

Every tool has a class the host's approval gate reads: `read`, `write` or
`always_approve`. `ToolRegistry` enforces `always_approve` itself: each call
goes to the registry's `approver(name, args, manifest)` callback and runs only
when it returns true. With no approver set, or one that raises, the call is
refused and the agent gets an error saying so. `read` and `write` tools are
left to the host.

```python
config, registry = build_agent_config(resolved, "chat")
registry.approver = lambda name, args, manifest: ask_a_human(name, args)
```

The registry also refuses a call that is missing a required argument before
it runs, and counts calls, failures, refusals and seconds per tool in
`registry.usage`. A `python` source call that runs past its `timeout_s`
(default 30) returns an error; the call keeps running in a background thread.

Defaults:

| Source | read | write | always_approve |
|---|---|---|---|
| OpenAPI and http | GET, HEAD, OPTIONS | other methods | DELETE, and any operation whose name or path sends messages (send, message, email, sms, notify, invite, publish), moves money (pay, payment, charge, refund, payout, transfer, checkout, purchase) or starts something that acts later (action, export, import, schedule, resume, activate, trigger, subscribe) |
| MCP | never by default | other tools | `destructiveHint: true`, or a name that deletes, sends or moves money |

An MCP server's `readOnlyHint` is a claim by the server, so it does not make a
tool `read`; grant that in the YAML if you trust it.

`--approval` is a floor: each tool gets at least that class and never less
than its own default, so `--approval read` changes nothing.

Tightening is always allowed: a spec's `x-fabri-approval` can raise a class,
and a host can raise classes for everyone with `approval_floor` in its policy
file:

```yaml
approval_floor: {"*": write, "crm_*": always_approve}
```

`credential_grants` in the same policy file caps which credentials each
source may use. Keys are source name patterns, values list `provider:handle`
refs or patterns:

```yaml
credential_grants: {"acme_*": ["slack:acme", "linear:acme"], "crm": ["crm:*"]}
```

A source whose headers or env name a ref outside its grants fails to build,
and its handlers resolve credentials through a store that refuses anything
else, so a layer edited at runtime cannot point a tool at another tenant's
token. Without the key there is no cap. `env:` header specs are not covered.

### Who answers an approval

`ToolRegistry` asks the `approver` for every `always_approve` call. Which approver a run has depends on the host:

- `fabri run` on a terminal prints the call on stderr and reads `[y/N]`. Only `y` or `yes` approves. Without a
  terminal (a pipe, CI) there is no approver and the call is refused.
- `fabri serve` and Studio send the call to the run's questions inbox as a question with approve and deny
  buttons. A run bound to Slack gets the same question there. Under strict auth an admin can answer any run, and the run's owner can answer their own run only when signed in as that owner. A run with no owner (submitted by token) is admin only. `POST /runs/<id>/answer` must carry the `question_id` of the question and an `answer`, or it gets a 400. For a credentialed `http`, `openapi` or `http` MCP source, the question comes from the credential broker and shows the rendered request. Only `always_approve` calls ask.
- A child agent asks the parent's human. Its own stdin is a pipe, so it can never approve its own call.
- A Python host passes `approver=` to `build_tools`, `registry(...)` or `build_agent_config`, or sets
  `registry.approver`, as in the example above.
- `FABRI_APPROVAL_TIMEOUT_S` sets how long a question waits (default 300 seconds). A timeout denies. `fabri serve` strips this variable from the runs it starts, so there the default applies unless `FABRI_RUN_ENV_ALLOW=FABRI_APPROVAL_TIMEOUT_S` re-admits it.
- No environment variable approves anything. There is no standing "yes".

Config-declared MCP servers under `tools.mcp_servers` get approval classes too, so a tool named like
`send_message` prompts or is refused in `fabri run`. Before, it ran with no class and no gate.

## Tool count and token cost

`fabri setup lint` and `GET /setups/lint` warn when a resolved setup has more than 40 tools or about 16000 tokens of
tool schema. At that point turn on tool retrieval or narrow `allow`. The thresholds are provisional until measured
on real layers. To see where the tokens go:

```sh
python scripts/measure_tool_tokens.py --layers baseline_dir pack_dir company_dir
```

It prints tokens, name and approval class per tool, biggest first, then the totals.

## MCP safety

- Text from an MCP server (tool descriptions, titles, schema strings, results) is cleaned of control characters,
  capped, and prefixed as data from the server. This does not stop every prompt injection. Treat a server you do
  not control as untrusted.
- An oversized schema, or two tools whose names collide after cleaning, is rejected.
- Values of your env vars and headers are removed from every error message. A server's stderr is never shown to the
  agent.
- A stdio source passes the child only a short list of host variables (`PATH`, `HOME`, locale, proxy and CA
  settings, and selected Docker settings: `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CERT_PATH`,
  `DOCKER_TLS_VERIFY`, `DOCKER_CONFIG`) plus the variables you name under `env`. This is `env_mode: allowlist`, the default for
  every source. A server that needs a key must declare it under `env`, for example
  `env: {OPENAI_API_KEY: {env: OPENAI_API_KEY}}`. `env_mode: inherit` is an explicit opt-in that passes the
  whole host environment; values whose names look like credentials are then redacted from what the server sends
  back, but that check goes by name and can miss one. A `tools.mcp_servers` entry works the same way, with
  `inherit_env: true` as its opt-in.
- MCP over HTTP refuses redirects and refuses private or loopback hosts unless `FABRI_FETCH_ALLOW_PRIVATE` is set.
  The private-host check happens before connecting, so DNS rebinding between the check and the connection is not
  closed.

## Sources in `fabri run`

`fabri run` builds tools from `tools.manifest_dir`, `tools.agents`, `tools.mcp_servers` and `tools.sources`.
`tools.sources` is a list of source declarations, the same mapping you would put under `sources:` in a setup's
`tools/sources/*.yaml` file (the format of `tools/tools.yaml`).

```yaml
tools:
  policy: policy.yaml        # optional; a file path or an inline mapping
  sources:
    - kind: openapi
      name: pets
      spec: specs/pets.yaml  # relative to the working directory
      headers: {Authorization: {env: PETS_TOKEN, prefix: "Bearer "}}
      methods: [GET]
```

- Each source goes through the same builder as a setup, so approval classes and the connector host floors apply.
- Without `tools.policy` the strict default applies: `openapi` and `http` sources only, no DELETE, no `python`.
  `tools.policy` takes a policy file path or an inline mapping, layered on that default. Its name allow list and
  `approval_floor` apply to these tools.
- Relative spec paths resolve against the working directory, like `tools.manifest_dir`.
- A source that fails to build is logged and skipped. A bad `tools.policy` raises.
- A source cannot replace a tool that is already registered, or the reserved `decompose` name. The tool is skipped
  with a warning.
- `tools.enabled` filters the result as it does for other tools, so list the source tool names there if you use it.

Two `http` source keys shape the request:

- `query: {uploadType: media}` sends these literal pairs on every call. They win over an argument of the same name.
- `body_format: rfc822` (POST or PUT, no path parameters) turns the arguments `to`, `cc`, `bcc`, `subject` and
  `body` into one plain text mail message sent as `Content-Type: message/rfc822`. `to`, `subject` and `body` are
  required, any other argument is refused, and a line break or control character in `to`, `cc`, `bcc` or `subject`
  is refused before anything is sent, so an argument cannot add a header. Under `fabri serve` the broker builds the
  message itself from those arguments, and the approval card shows the recipients, the subject and the body as text.
  The Gmail connector's send and draft tools use it.

Loosening is possible only by editing the YAML by hand (`approval: {pet_delete:
read}`), and then `fabri setup lint`, the add commands and `GET /setups/lint`
warn that the class is looser than the default.

## Managing sources

```sh
fabri setups tools list --scope org:acme            # NAME KIND STATE TOOLS(read/write/always) FILE
fabri setups tools disable pets --scope org:acme    # writes enabled: false
fabri setups tools enable pets --scope org:acme
fabri setups tools remove pets --scope org:acme     # deletes the YAML and its tools/specs copy
fabri setups tools sync --scope org:acme            # added, removed, changed since the last sync
fabri setups tools probe --scope org:acme --ops pet_list   # safe GETs, saves options and examples
```

`list` also shows sources declared in `tools/tools.yaml` and registered tools.
`enable`, `disable` and `remove` work on `tools/sources` files and registered
tools; a source in `tools.yaml` is changed by editing that file.

Every add, enable, disable and remove appends a line to the layer's
`.fabri-audit.jsonl` with the actor (`--actor`, else `$USER`).

## Over HTTP

The same operations are on the Setups API for Studio and scripts:
`GET /tools/sources`, `POST /tools/sources`, `POST /tools/sources/enable`,
`DELETE /tools/sources` and `GET /tools/connectors`. See
[setups-api.md](setups-api.md).

```sh
curl -X POST localhost:8080/tools/sources -H "Authorization: Bearer $FABRI_ADMIN_TOKEN" \
  -d '{"scope": "org:acme", "source": "connector", "connector": "slack", "workspace": "T0123", "dry_run": true}'
```
