# Adding tools to a setup

Three kinds of tool source can be added to any scope with one command:

- an OpenAPI 3 spec (a file or a URL)
- an MCP server (a stdio command or an HTTP URL)
- a connector (GitHub, Linear, Slack)

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
```

| Connector | Source kind | Default credential | What you get |
|---|---|---|---|
| `github` | mcp (Docker, or `--remote`) | `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub's official MCP server: issues, PRs, code, actions |
| `linear` | mcp (hosted) | `LINEAR_API_KEY` | Linear's MCP server: issues, projects, comments |
| `slack` | http | `SLACK_BOT_TOKEN` | `slack_channels_list`, `slack_channel_history` (read), `slack_post_message` (always_approve) |

`--workspace` uses the credential store ref `<provider>:<workspace>`, which
fabri resolves from `FABRI_CRED_<PROVIDER>_<WORKSPACE>` or from the Slack and
Linear install database. Connectors do not reach the service when added; pass
`--connect` to list the tools of an MCP connector now. A missing token here is
a warning, not an error: the file is right and the tools build where the
token is set. The github and linear connectors need `mcp` in `allowed_kinds`.

## Approval classes

Every tool has a class the host's approval gate reads: `read`, `write` or
`always_approve`. Defaults:

| Source | read | write | always_approve |
|---|---|---|---|
| OpenAPI and http | GET, HEAD, OPTIONS | other methods | DELETE, and any operation whose name or path sends messages (send, message, email, sms, notify, invite, publish) or moves money (pay, payment, charge, refund, payout, transfer, checkout, purchase) |
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
